#Requires -Version 5.1
<#
.SYNOPSIS
    xhs-agent · 状态总览（容器 / 进程 / 健康 / 计划任务 / 最近日志）

.DESCRIPTION
    只读脚本：不启动、不停止、不改动任何东西。输出只使用项目相对路径。
    健康检查直接复用 scripts\healthcheck.py（本机回环四项 + 前端代理）。

    典型用法：
        powershell -ExecutionPolicy Bypass -File scripts\ops\status.ps1
        powershell -ExecutionPolicy Bypass -File scripts\ops\status.ps1 -NoHealth

    退出码：0 = 报告完成（本脚本只做展示，不代表服务健康；健康结论看输出）
            1 = 脚本自身意外错误
#>
[CmdletBinding()]
param(
    [string]$Root = '',
    [switch]$NoHealth
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ops-common.ps1')
Initialize-OpsConsole

try {
    $Root = Resolve-OpsRoot -ScriptRoot $PSScriptRoot -Override $Root
    $logsDir = Get-OpsLogsDir -Root $Root

    Write-Host '=================================================='
    Write-Host ' xhs-agent · 状态总览'
    Write-Host (' 时间：' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    Write-Host '=================================================='

    # ============================ 容器 ============================
    Write-OpsStep '容器（docker compose）'
    if (-not (Get-OpsDockerExe)) {
        Write-OpsBad ' 未检测到 docker 命令（Docker Desktop 未安装 / 不在 PATH）。'
    } elseif (-not (Test-OpsDockerEngine)) {
        Write-OpsWarn ' Docker CLI 存在，但引擎未运行（请启动 Docker Desktop）。'
    } else {
        foreach ($svc in @(
                @{ label = 'db '; container = 'xhs-db' },
                @{ label = 'redis'; container = 'xhs-redis' },
                @{ label = 'dailyhot'; container = 'xhs-dailyhot' }
            )) {
            $state = Get-OpsContainerHealth -Name $svc.container
            $text = "$($svc.label)  $($svc.container)  →  $state"
            if ($state -eq 'healthy' -or $state -eq 'running') {
                Write-Host ('  [OK] ' + $text) -ForegroundColor Green
            } elseif ($svc.container -eq 'xhs-dailyhot') {
                Write-Host ('  [!]  ' + $text + '（容错：热搜刷新暂不可用，不影响核心功能）') -ForegroundColor Yellow
            } else {
                Write-Host ('  [X]  ' + $text) -ForegroundColor Red
            }
        }
    }

    # ============================ 进程 ============================
    Write-OpsStep '宿主机进程（logs\run-*.json 记录）'
    $anyProcess = $false
    foreach ($svc in @(@{ name = 'backend'; label = '后端'; port = 8000 }, @{ name = 'frontend'; label = '前端'; port = 3000 })) {
        $state = Read-OpsRunState -LogsDir $logsDir -Service $svc.name
        if ($null -eq $state) {
            Write-OpsInfo ("$($svc.label)：未在运行（无记录）")
            continue
        }
        $anyProcess = $true
        $pidValue = [int]$state.pid
        $alive = Test-OpsPidAlive -ProcessId $pidValue
        $listening = (@(Get-OpsPortPids -Port $svc.port)).Count -gt 0
        $mode = [string]$state.mode
        $restarts = ''
        if ($state.PSObject.Properties['restart_count'] -and $state.restart_count) {
            $restarts = '，重启 ' + $state.restart_count + ' 次'
        }
        if ($alive -and $listening) {
            Write-Host ("  [OK] $($svc.label)：PID $pidValue，端口 $($svc.port) 监听中（$mode 模式$restarts）") -ForegroundColor Green
        } elseif ($alive) {
            Write-Host ("  [!]  $($svc.label)：PID $pidValue 存活，但端口 $($svc.port) 未监听（可能启动中或异常）") -ForegroundColor Yellow
        } else {
            Write-Host ("  [X]  $($svc.label)：记录的 PID $pidValue 已不存在（陈旧记录，可重新 start）") -ForegroundColor Red
        }
        if ($state.log) { Write-OpsInfo ('日志：' + [string]$state.log) }
    }

    # 端口占用（非本项目进程也要能看出；记录过的 PID + 其子进程树视为「本项目」）
    $recordedPids = @()
    foreach ($svcName in @('backend', 'frontend', 'watch')) {
        $st = Read-OpsRunState -LogsDir $logsDir -Service $svcName
        if ($null -ne $st) { $recordedPids += [int]$st.pid }
    }
    foreach ($port in @(8000, 3000)) {
        $holders = @(Get-OpsPortPids -Port $port)
        if ($holders.Count -eq 0) { continue }
        $foreign = @()
        foreach ($hp in $holders) {
            if (-not (Test-OpsPortOwnedByProject -Root $Root -ProcessId $hp -RecordedPids $recordedPids)) { $foreign += $hp }
        }
        if ($foreign.Count -gt 0) {
            Write-OpsWarn ("端口 $port 存在非本项目进程占用（PID " + ($foreign -join ', ') + '）')
        }
    }

    $watchState = Read-OpsRunState -LogsDir $logsDir -Service 'watch'
    if ($null -ne $watchState) {
        $watchAlive = Test-OpsPidAlive -ProcessId ([int]$watchState.pid)
        if ($watchAlive) {
            Write-Host ("  [OK] 常驻监控（-Watch）：PID " + [int]$watchState.pid + ' 运行中') -ForegroundColor Green
        } else {
            Write-Host ('  [!]  常驻监控（-Watch）：记录存在但进程已退出') -ForegroundColor Yellow
        }
    } else {
        Write-OpsInfo '常驻监控（-Watch）：未开启'
    }

    # ============================ 健康检查 ============================
    Write-OpsStep '健康检查（scripts\healthcheck.py）'
    if ($NoHealth) {
        Write-OpsInfo '已按 -NoHealth 跳过。'
    } else {
        $py = Get-OpsPythonExe -Root $Root
        $hcr = Invoke-OpsCapture -Exe $py -Arguments @((Join-Path $Root 'scripts\healthcheck.py')) -WorkingDirectory $Root
        $output = ($hcr.output -join "`n")
        $hcRc = $hcr.rc
        Write-Host $output.TrimEnd()
        if ($hcRc -eq 0) {
            Write-OpsOk '健康检查全绿'
        } else {
            Write-OpsWarn '健康检查未通过（明细见上方；先确认 start 是否已运行）'
        }
    }

    # ============================ 计划任务（可撤销项） ============================
    Write-OpsStep '可撤销项（计划任务）'
    foreach ($item in @(
            @{ name = 'autostart'; label = '开机自启' },
            @{ name = 'backup'; label = '定时备份' }
        )) {
        $task = Get-OpsScheduledTask -TaskName ('xhs-agent-' + $item.name)
        if ($null -ne $task) {
            Write-Host ("  [OK] $($item.label)：已注册（" + $task.State + "，任务名 xhs-agent\$($task.TaskName)）") -ForegroundColor Green
        } else {
            Write-OpsInfo ("$($item.label)：未注册（默认不启用；安装见 docs\operations.md）")
        }
    }
    Write-OpsInfo '卸载：scripts\ops\uninstall-autostart.ps1 / uninstall-backup-task.ps1（删除即复原）'

    # ============================ 最近日志 ============================
    Write-OpsStep '最近日志（logs\）'
    $patterns = @(
        @{ tag = '后端输出'; pattern = 'backend-*.log'; excludeErr = $true; excludeBuild = $false },
        @{ tag = '后端错误'; pattern = 'backend-*.err.log'; excludeErr = $false; excludeBuild = $false },
        @{ tag = '前端输出'; pattern = 'frontend-*.log'; excludeErr = $true; excludeBuild = $true },
        @{ tag = '前端错误'; pattern = 'frontend-*.err.log'; excludeErr = $false; excludeBuild = $false },
        @{ tag = '前端构建'; pattern = 'frontend-build-*.log'; excludeErr = $false; excludeBuild = $false },
        @{ tag = '常驻监控'; pattern = 'watch-*.log'; excludeErr = $false; excludeBuild = $false },
        @{ tag = '备份历史'; pattern = 'backup-history.log'; excludeErr = $false; excludeBuild = $false }
    )
    $found = $false
    foreach ($p in $patterns) {
        $files = @(Get-ChildItem -LiteralPath $logsDir -Filter $p.pattern -File -ErrorAction SilentlyContinue)
        if ($p.excludeErr) { $files = @($files | Where-Object { $_.Name -notlike '*.err.log' }) }
        if ($p.excludeBuild) { $files = @($files | Where-Object { $_.Name -notlike 'frontend-build-*' }) }
        $file = @($files | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
        if ($file.Count -gt 0) {
            $found = $true
            $rel = Get-OpsRelPath -Root $Root -Path $file[0].FullName
            Write-OpsInfo ("$($p.tag)：$rel（最后写入 " + $file[0].LastWriteTime.ToString('MM-dd HH:mm:ss') + '）')
        }
    }
    if (-not $found) {
        Write-OpsInfo '暂无日志（首次启动后会生成）。'
    }

    Write-Host ''
    Write-OpsOk '状态报告完成。'
    exit 0
} catch {
    Write-OpsBad ('状态脚本出现意外错误：' + (Get-OpsSafeText -Text $_.Exception.Message))
    exit 1
}
