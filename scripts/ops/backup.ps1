#Requires -Version 5.1
<#
.SYNOPSIS
    xhs-agent · 数据备份（容器内 pg_dump -Fc → 宿主机 backups\，默认保留最近 7 份）

.DESCRIPTION
    流程：
      1) 确认 Docker 引擎与 xhs-db 容器 healthy（不健康 → 拒绝备份，退出码 3）；
      2) docker compose exec -T db pg_dump -U postgres -d xhs_agent -Fc
         （-Fc 自定义压缩格式，可用 pg_restore 恢复；不经过宿主机管道，二进制零截断）；
      3) 容器内先做完整性校验（文件非空 + pg_restore -l 可列出对象）→ 再拷出到 backups\；
      4) 按文件名（时间戳）保留最近 N 份，更旧的自动清理并打印清单。

    ⚠️ backend\.env【不在备份范围内】（含密钥；如需单独备份请自行加密保管）。
    ⚠️ 本脚本只读数据库、只写 backups\ 与 logs\backup-history.log，绝不删除数据卷。

    典型用法：
        powershell -ExecutionPolicy Bypass -File scripts\ops\backup.ps1
        powershell -ExecutionPolicy Bypass -File scripts\ops\backup.ps1 -RetentionCount 14

    退出码约定：
        0 = 备份成功（含保留清理）
        1 = 备份或校验失败
        3 = Docker / 数据库未就绪
#>
[CmdletBinding()]
param(
    [string]$Root = '',
    [string]$OutputDir = 'backups',
    [int]$RetentionCount = 7,
    [string]$Database = 'xhs_agent'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ops-common.ps1')
Initialize-OpsConsole

$historyLine = ''
try {
    $Root = Resolve-OpsRoot -ScriptRoot $PSScriptRoot -Override $Root
    $logsDir = Get-OpsLogsDir -Root $Root

    Write-Host '=================================================='
    Write-Host ' xhs-agent · 数据备份（pg_dump -Fc）'
    Write-Host '=================================================='

    # ============================ 1/4 前置检查 ============================
    Write-OpsStep '1/4 前置检查'
    if (-not (Get-OpsDockerExe)) {
        Write-OpsBad '未检测到 docker 命令。请先运行 scripts\install-docker.ps1。'
        exit 3
    }
    if (-not (Test-OpsDockerEngine)) {
        Write-OpsBad 'Docker 引擎未运行。请先启动 Docker Desktop（或运行 scripts\ops\start.ps1）。'
        exit 3
    }
    $dbState = Get-OpsContainerHealth -Name 'xhs-db'
    if ($dbState -ne 'healthy' -and $dbState -ne 'running') {
        Write-OpsBad ("xhs-db 未就绪（当前：" + $dbState + '）。')
        Write-OpsInfo '请先启动：scripts\ops\start.ps1（或 docker compose up -d db）'
        exit 3
    }
    Write-OpsOk 'Docker 引擎与 xhs-db 就绪'

    # ============================ 2/4 执行 pg_dump ============================
    Write-OpsStep '2/4 导出数据库'
    $outDirPath = $OutputDir
    if (-not [System.IO.Path]::IsPathRooted($outDirPath)) {
        $outDirPath = Join-Path $Root $OutputDir
    }
    if (-not (Test-Path -LiteralPath $outDirPath)) {
        New-Item -ItemType Directory -Path $outDirPath | Out-Null
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $fileName = "$Database-$stamp.dump"
    $containerTmp = "/tmp/$fileName"
    $localPath = Join-Path $outDirPath $fileName

    $dump = Invoke-OpsCompose -Root $Root -Arguments @('exec', '-T', 'db', 'pg_dump', '-U', 'postgres', '-d', $Database, '-Fc', '-f', $containerTmp)
    if ($dump.rc -ne 0) {
        Write-OpsBad ('pg_dump 执行失败（退出码 ' + $dump.rc + '）。')
        foreach ($line in @($dump.output | Select-Object -Last 6)) { Write-OpsInfo ([string]$line) }
        $historyLine = ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' | FAIL | pg_dump rc=' + $dump.rc)
        exit 1
    }

    # 容器内校验：大小 + pg_restore -l 可列出对象（防半截文件）
    $sizeOut = Invoke-OpsCompose -Root $Root -Arguments @('exec', '-T', 'db', 'stat', '-c', '%s', $containerTmp)
    $sizeBytes = 0
    if ($sizeOut.rc -eq 0 -and $sizeOut.output.Count -gt 0) {
        [void][long]::TryParse(([string]$sizeOut.output[0]).Trim(), [ref]$sizeBytes)
    }
    if ($sizeBytes -le 0) {
        Write-OpsBad '容器内备份文件为空（校验未通过），本次视为失败。'
        [void](Invoke-OpsCompose -Root $Root -Arguments @('exec', '-T', 'db', 'rm', '-f', $containerTmp))
        $historyLine = ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' | FAIL | 容器内文件为空')
        exit 1
    }

    $toc = Invoke-OpsCompose -Root $Root -Arguments @('exec', '-T', 'db', 'pg_restore', '-l', $containerTmp)
    $tocEntries = @($toc.output | Where-Object { $_ -match ';' }).Count
    if ($toc.rc -ne 0 -or $tocEntries -lt 10) {
        Write-OpsBad ('pg_restore -l 校验未通过（可读条目 ' + $tocEntries + ' 条），拒绝产出无效备份。')
        [void](Invoke-OpsCompose -Root $Root -Arguments @('exec', '-T', 'db', 'rm', '-f', $containerTmp))
        $historyLine = ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' | FAIL | pg_restore -l 条目不足')
        exit 1
    }
    Write-OpsOk ('导出完成并通过完整性校验：' + [math]::Round($sizeBytes / 1024, 1) + ' KB，归档条目 ' + $tocEntries + ' 项')

    # 拷出到宿主机（避免二进制经管道；docker cp 按容器名）
    $dockerExe = Get-OpsDockerExe
    $cp = Invoke-OpsCapture -Exe $dockerExe -Arguments @('cp', ("xhs-db:" + $containerTmp), $localPath)
    [void](Invoke-OpsCompose -Root $Root -Arguments @('exec', '-T', 'db', 'rm', '-f', $containerTmp))
    if ($cp.rc -ne 0 -or -not (Test-Path -LiteralPath $localPath)) {
        Write-OpsBad '从容器拷出备份文件失败。'
        foreach ($line in @($cp.output | Select-Object -Last 4)) { Write-OpsInfo ([string]$line) }
        $historyLine = ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' | FAIL | docker cp 失败')
        exit 1
    }
    $localSize = (Get-Item -LiteralPath $localPath).Length
    if ($localSize -le 0) {
        Write-OpsBad '宿主机备份文件为空，本次视为失败。'
        [void](Remove-Item -LiteralPath $localPath -Force -ErrorAction SilentlyContinue)
        $historyLine = ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' | FAIL | 宿主机文件为空')
        exit 1
    }
    $relPath = Get-OpsRelPath -Root $Root -Path $localPath

    # ============================ 3/4 保留策略 ============================
    Write-OpsStep ('3/4 保留策略（最近 ' + ([Math]::Max(1, $RetentionCount)) + ' 份）')
    $keep = [Math]::Max(1, $RetentionCount)
    $pattern = "$Database-*.dump"
    $all = @(Get-ChildItem -LiteralPath $outDirPath -Filter $pattern -File -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending)
    $keptFiles = @($all | Select-Object -First $keep)
    $deletedFiles = @($all | Select-Object -Skip $keep)
    foreach ($f in $keptFiles) {
        Write-OpsInfo ('保留：' + (Get-OpsRelPath -Root $Root -Path $f.FullName))
    }
    foreach ($f in $deletedFiles) {
        try {
            Remove-Item -LiteralPath $f.FullName -Force
            Write-OpsInfo ('清理：' + (Get-OpsRelPath -Root $Root -Path $f.FullName))
        } catch {
            Write-OpsWarn ('清理失败（跳过）：' + (Get-OpsRelPath -Root $Root -Path $f.FullName))
        }
    }

    # ============================ 4/4 摘要 ============================
    Write-OpsStep '4/4 摘要'
    Write-OpsOk ('备份完成：' + $relPath + '（' + [math]::Round($localSize / 1024, 1) + ' KB）')
    Write-OpsInfo ('保留 ' + $keptFiles.Count + ' 份 / 本次清理 ' + $deletedFiles.Count + ' 份')
    Write-OpsInfo '说明：backend\.env 含密钥，默认不纳入备份；恢复请用 scripts\ops\restore.ps1（先验证到临时库）。'
    Write-OpsInfo '恢复示例（先验证到临时库）：'
    Write-OpsInfo ('    powershell -ExecutionPolicy Bypass -File scripts\ops\restore.ps1 -File ' + $relPath)

    $historyLine = ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' | OK | ' + $relPath + ' | ' +
        [math]::Round($localSize / 1024, 1) + ' KB | kept=' + $keptFiles.Count + ' deleted=' + $deletedFiles.Count)
    Write-Output ''
    exit 0
} catch {
    $historyLine = ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' | FAIL | ' + (Get-OpsSafeText -Text $_.Exception.Message))
    Write-OpsBad ('备份过程出现意外错误：' + (Get-OpsSafeText -Text $_.Exception.Message))
    exit 1
} finally {
    if ($historyLine -and $Root) {
        try {
            $histPath = Join-Path (Get-OpsLogsDir -Root $Root) 'backup-history.log'
            Add-Content -LiteralPath $histPath -Value $historyLine -Encoding UTF8
        } catch { }
    }
}
