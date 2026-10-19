#Requires -Version 5.1
<#
.SYNOPSIS
    xhs-agent · 数据恢复（默认恢复到【临时验证库】，-Force 才允许覆盖 xhs_agent）

.DESCRIPTION
    安全设计（防误覆盖）：
      - 默认行为：把备份恢复到一个**新建的临时库**（xhs_restore_verify_时间戳）做验证，
        不触碰 xhs_agent；完成后打印核对方法；
      - 覆盖主库：必须显式 -Force，且需在所交互确认（或再加 -Yes 用于脚本化），
        覆盖前对现库执行 DROP DATABASE ... WITH (FORCE) —— 不可恢复，请谨慎；
      - 恢复后自动核对：public 表数量 / vector 扩展 / langgraph 框架表 / 关键表行数比对。

    典型用法：
        # 1) 验证性恢复（推荐先跑这个）
        powershell -ExecutionPolicy Bypass -File scripts\ops\restore.ps1 -File backups\xhs_agent-20261013-210000.dump
        # 2) 验证后删除临时库
        powershell -ExecutionPolicy Bypass -File scripts\ops\restore.ps1 -File backups\....dump -DropVerifyDb
        # 3) 覆盖主库（危险操作，二次确认）
        powershell -ExecutionPolicy Bypass -File scripts\ops\restore.ps1 -File backups\....dump -Force

    退出码约定：
        0 = 恢复（含验证）成功
        1 = 恢复出错或校验不通过
        2 = 用户放弃 / 目标库名冲突 / 参数非法
        3 = Docker / 数据库未就绪
#>
[CmdletBinding()]
param(
    [string]$File = '',
    [string]$Root = '',
    [string]$TargetDatabase = '',
    [switch]$Force,
    [switch]$Yes,
    [switch]$DropVerifyDb
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ops-common.ps1')
Initialize-OpsConsole

# 关键表（核对用；与第 18/19 步「业务表」口径一致）
$KEY_TABLES = @('accounts', 'personas', 'notes', 'topics', 'note_metrics', 'diagnostics', 'covers', 'hotspots', 'knowledge_snippets')

function Invoke-PsqlScalar {
    param(
        [string]$Root,
        [string]$Database,
        [string]$Sql
    )
    $r = Invoke-OpsCompose -Root $Root -Arguments @('exec', '-T', 'db', 'psql', '-U', 'postgres', '-d', $Database, '-tAc', $Sql)
    if ($r.rc -ne 0 -or $r.output.Count -eq 0) { return $null }
    return ([string]$r.output[0]).Trim()
}

try {
    $Root = Resolve-OpsRoot -ScriptRoot $PSScriptRoot -Override $Root
    Write-Host '=================================================='
    Write-Host ' xhs-agent · 数据恢复（pg_restore）'
    Write-Host '=================================================='

    # ============================ 1/6 参数与前置检查 ============================
    Write-OpsStep '1/6 参数与前置检查'
    if (-not $File) {
        Write-OpsBad '缺少 -File 参数（备份文件路径，例如 backups\xhs_agent-20261013-210000.dump）。'
        exit 2
    }
    $filePath = $File
    if (-not [System.IO.Path]::IsPathRooted($filePath)) {
        $filePath = Join-Path $Root $File
    }
    if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
        Write-OpsBad ('备份文件不存在：' + (Get-OpsRelPath -Root $Root -Path $filePath))
        exit 2
    }
    $fileSize = (Get-Item -LiteralPath $filePath).Length
    if ($fileSize -le 0) {
        Write-OpsBad '备份文件为空。'
        exit 2
    }
    $fileRel = Get-OpsRelPath -Root $Root -Path $filePath

    if (-not (Get-OpsDockerExe)) { Write-OpsBad '未检测到 docker 命令。'; exit 3 }
    if (-not (Test-OpsDockerEngine)) { Write-OpsBad 'Docker 引擎未运行。'; exit 3 }
    $dbState = Get-OpsContainerHealth -Name 'xhs-db'
    if ($dbState -ne 'healthy' -and $dbState -ne 'running') {
        Write-OpsBad ("xhs-db 未就绪（当前：" + $dbState + '）。')
        exit 3
    }
    Write-OpsOk ('备份文件：' + $fileRel + '（' + [math]::Round($fileSize / 1024, 1) + ' KB）')

    # ============================ 2/6 目标库决策 ============================
    Write-OpsStep '2/6 目标库决策'
    $target = ''
    $isOverwriteMain = $false
    if ($Force) {
        $target = if ($TargetDatabase) { $TargetDatabase } else { 'xhs_agent' }
        $isOverwriteMain = ($target -eq 'xhs_agent')
    } elseif ($TargetDatabase) {
        $target = $TargetDatabase
    } else {
        $target = 'xhs_restore_verify_' + (Get-Date -Format 'yyyyMMdd_HHmmss')
    }
    if ($target -notmatch '^[A-Za-z0-9_]{1,60}$') {
        Write-OpsBad ('目标库名非法（仅允许字母 / 数字 / 下划线，≤60 字符）：' + $target)
        exit 2
    }

    if ($isOverwriteMain) {
        Write-OpsWarn '⚠️ 覆盖模式：将把当前 xhs_agent 库【删除并重建】后恢复备份 —— 现有数据不可恢复！'
        if (-not $Yes) {
            Write-Host ''
            $answer = Read-Host '确认覆盖请输入 YES（其他任何输入都会放弃）'
            if ($answer -ne 'YES') {
                Write-OpsBad '未确认，已放弃（未做任何改动）。'
                exit 2
            }
        }
    } else {
        Write-OpsOk ('恢复目标：临时验证库 ' + $target + '（不影响 xhs_agent）')
    }

    # 目标库是否已存在
    $exists = Invoke-PsqlScalar -Root $Root -Database 'postgres' -Sql ("SELECT 1 FROM pg_database WHERE datname = '" + $target + "'")
    if ($exists -eq '1') {
        if ($isOverwriteMain) {
            Write-OpsWarn ('目标库 ' + $target + ' 已存在：按覆盖模式执行 DROP DATABASE ... WITH (FORCE)')
            $drop = Invoke-OpsCompose -Root $Root -Arguments @('exec', '-T', 'db', 'psql', '-U', 'postgres', '-c', ('DROP DATABASE "' + $target + '" WITH (FORCE);'))
            if ($drop.rc -ne 0) {
                Write-OpsBad 'DROP DATABASE 失败（可能有连接占用 / 权限问题）。'
                foreach ($line in @($drop.output | Select-Object -Last 4)) { Write-OpsInfo ([string]$line) }
                exit 1
            }
        } else {
            Write-OpsBad ('目标库 ' + $target + ' 已存在。请换 -TargetDatabase 名称，或先手动删除该库。')
            exit 2
        }
    }

    # ============================ 3/6 拷入容器并恢复 ============================
    Write-OpsStep '3/6 执行恢复'
    $baseName = [System.IO.Path]::GetFileName($filePath)
    $containerTmp = '/tmp/' + $baseName
    $dockerExe = Get-OpsDockerExe
    $cp = Invoke-OpsCapture -Exe $dockerExe -Arguments @('cp', $filePath, ("xhs-db:" + $containerTmp))
    if ($cp.rc -ne 0) {
        Write-OpsBad '备份文件拷入容器失败。'
        exit 1
    }

    $create = Invoke-OpsCompose -Root $Root -Arguments @('exec', '-T', 'db', 'psql', '-U', 'postgres', '-c', ('CREATE DATABASE "' + $target + '";'))
    if ($create.rc -ne 0) {
        Write-OpsBad 'CREATE DATABASE 失败。'
        [void](Invoke-OpsCompose -Root $Root -Arguments @('exec', '-T', 'db', 'rm', '-f', $containerTmp))
        exit 1
    }

    $restore = Invoke-OpsCompose -Root $Root -Arguments @('exec', '-T', 'db', 'pg_restore', '-U', 'postgres', '-d', $target, '--no-owner', $containerTmp)
    [void](Invoke-OpsCompose -Root $Root -Arguments @('exec', '-T', 'db', 'rm', '-f', $containerTmp))
    $warnCount = @($restore.output | Where-Object { $_ -match 'warning|警告|error|错误' }).Count
    if ($restore.rc -ge 2) {
        Write-OpsBad ('pg_restore 失败（退出码 ' + $restore.rc + '）。')
        foreach ($line in @($restore.output | Select-Object -First 8)) { Write-OpsInfo ([string]$line) }
        exit 1
    } elseif ($restore.rc -eq 1) {
        Write-OpsWarn ('pg_restore 完成但有警告（退出码 1，共 ' + $warnCount + ' 条相关输出）；继续做结构核对。')
    } else {
        Write-OpsOk 'pg_restore 完成（退出码 0）'
    }

    # ============================ 4/6 结构与行数核对 ============================
    Write-OpsStep '4/6 恢复结果核对'
    $verifyFailed = $false

    $publicTables = Invoke-PsqlScalar -Root $Root -Database $target -Sql "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
    $langgraphTables = Invoke-PsqlScalar -Root $Root -Database $target -Sql "SELECT count(*) FROM pg_tables WHERE schemaname='langgraph'"
    $hasVector = Invoke-PsqlScalar -Root $Root -Database $target -Sql "SELECT count(*) FROM pg_extension WHERE extname='vector'"
    $hasAlembic = Invoke-PsqlScalar -Root $Root -Database $target -Sql "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename='alembic_version'"

    if ([int]$publicTables -ge 11) { Write-OpsOk ('public 表数量：' + $publicTables + '（含 alembic_version）') }
    else { Write-OpsBad ('public 表数量异常：' + $publicTables); $verifyFailed = $true }

    if ($hasAlembic -eq '1') { Write-OpsOk 'alembic_version 表存在' } else { Write-OpsBad 'alembic_version 表缺失'; $verifyFailed = $true }
    if ($hasVector -eq '1') { Write-OpsOk 'vector 扩展存在' } else { Write-OpsBad 'vector 扩展缺失'; $verifyFailed = $true }
    if ([int]$langgraphTables -ge 4) { Write-OpsOk ('langgraph 框架表：' + $langgraphTables + ' 张') }
    else { Write-OpsWarn ('langgraph 框架表：' + $langgraphTables + ' 张（备份里可能不含检查点表）') }

    if (-not $isOverwriteMain) {
        $mismatch = @()
        foreach ($t in $KEY_TABLES) {
            $srcCount = Invoke-PsqlScalar -Root $Root -Database 'xhs_agent' -Sql ("SELECT count(*) FROM " + $t)
            $dstCount = Invoke-PsqlScalar -Root $Root -Database $target -Sql ("SELECT count(*) FROM " + $t)
            if ($srcCount -ne $dstCount) { $mismatch += ($t + '(' + $srcCount + '≠' + $dstCount + ')') }
        }
        if ($mismatch.Count -eq 0) {
            Write-OpsOk ('关键表行数与源库一致（' + $KEY_TABLES.Count + ' 张比对通过）')
        } else {
            Write-OpsWarn ('关键表行数与源库不一致：' + ($mismatch -join ', ') + '（若源库正在写入属正常；请人工复核）')
        }
    }

    # ============================ 5/6 临时库收尾 ============================
    Write-OpsStep '5/6 收尾'
    if ($DropVerifyDb) {
        if ($isOverwriteMain) {
            Write-OpsInfo '当前为覆盖主库模式，-DropVerifyDb 不适用（已忽略）。'
        } else {
            $dropR = Invoke-OpsCompose -Root $Root -Arguments @('exec', '-T', 'db', 'psql', '-U', 'postgres', '-c', ('DROP DATABASE "' + $target + '";'))
            if ($dropR.rc -eq 0) { Write-OpsOk ('临时验证库已删除：' + $target) }
            else { Write-OpsWarn ('临时库删除失败（可手动删除）：' + $target) }
        }
    } else {
        if (-not $isOverwriteMain) {
            Write-OpsInfo ('临时验证库已保留：' + $target)
            Write-OpsInfo '核对完成后删除：'
            Write-OpsInfo ('    docker compose exec -T db psql -U postgres -c "DROP DATABASE ' + $target + '"')
        }
    }

    # ============================ 6/6 结果 ============================
    Write-OpsStep '6/6 结果'
    if ($verifyFailed) {
        Write-OpsBad '恢复完成但结构核对未通过 —— 请勿在生产/主库使用该备份，先排查。'
        exit 1
    }
    Write-Host ''
    Write-OpsOk '恢复成功（验证通过）。'
    if ($isOverwriteMain) {
        Write-OpsInfo '提示：如后端正在运行，可运行 scripts\ops\start.ps1 复核健康检查。'
    } else {
        Write-OpsInfo '这是验证副本，主库 xhs_agent 未被改动。'
        Write-OpsInfo '如需替换主库：先停后端（stop.bat），再以 -Force 重跑覆盖模式（危险操作，请自行确认）。'
    }
    exit 0
} catch {
    Write-OpsBad ('恢复过程出现意外错误：' + (Get-OpsSafeText -Text $_.Exception.Message))
    exit 1
}
