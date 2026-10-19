#Requires -Version 5.1
<#
.SYNOPSIS
    xhs-agent · 一键停止（优雅停止前后端进程；容器默认保留）

.DESCRIPTION
    按 logs\run-*.json 的记录停止后端 / 前端：
      1) 先发关闭信号（taskkill 不带 /F）→ 等待 → 超时再强杀（/T 覆盖子进程树）；
      2) 校验 PID 未被系统复用（启动时刻比对，不误杀他人进程）；
      3) 确认 8000 / 3000 端口释放；残留属本项目的孤儿进程会被清理，
         属外部进程则只报告、不干预。
    若有 -Watch 常驻监控在运行：先写「停止请求」标记，监控不再重启服务，
    并等待监控进程退出。

    典型用法：
        powershell -ExecutionPolicy Bypass -File scripts\ops\stop.ps1
        powershell -ExecutionPolicy Bypass -File scripts\ops\stop.ps1 -DownContainers
        双击根目录 stop.bat（等价于默认：只停宿主机进程，保留容器与数据）

    开关：
        -DownContainers   一并执行 docker compose down（**绝不**携带卷删除参数 -v；
                          命名卷 xhs-agent-pgdata / xhs-agent-redis-data 会保留）
        -Root <dir>       指定项目根（默认由脚本位置推断；供自检注入）

    退出码约定：
        0 = 已停止（端口均已释放，或本就没有在运行）
        1 = 存在未能释放的端口（被非本项目进程占用）或容器停止失败
#>
[CmdletBinding()]
param(
    [switch]$DownContainers,
    [string]$Root = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ops-common.ps1')
Initialize-OpsConsole

try {
    $Root = Resolve-OpsRoot -ScriptRoot $PSScriptRoot -Override $Root
    $logsDir = Get-OpsLogsDir -Root $Root
    $stopMarker = Join-Path $logsDir 'stop.request'
    $exitCode = 0

    Write-Host '=================================================='
    Write-Host ' xhs-agent · 一键停止'
    Write-Host '=================================================='

    # 停止请求标记：让 -Watch 常驻监控「不再重启」并自行退出
    try { Set-Content -LiteralPath $stopMarker -Value ((Get-Date).ToString('o')) -Encoding UTF8 } catch { }

    try {
        # ============================ 停止前后端进程 ============================
        Write-OpsStep '停止后端与前端进程'

        # 端口归属判定用：先收集全部记录 PID（前端/后端/监控），供残留清理识别「我们的进程」
        $recordedPids = @()
        foreach ($svcName in @('backend', 'frontend', 'watch')) {
            $st = Read-OpsRunState -LogsDir $logsDir -Service $svcName
            if ($null -ne $st) { $recordedPids += [int]$st.pid }
        }

        foreach ($svc in @(@{ name = 'backend'; label = '后端'; port = 8000 }, @{ name = 'frontend'; label = '前端'; port = 3000 })) {
            $state = Read-OpsRunState -LogsDir $logsDir -Service $svc.name
            if ($null -eq $state) {
                Write-OpsInfo ("$($svc.label)：未找到运行记录（可能未通过 start 启动，或已停止）")
                continue
            }
            $pidValue = [int]$state.pid
            if (-not (Test-OpsPidAlive -ProcessId $pidValue)) {
                Write-OpsInfo ("$($svc.label)：进程已不在（PID $pidValue，可能已退出）")
                Remove-OpsRunState -LogsDir $logsDir -Service $svc.name
                continue
            }

            # PID 复用防护：启动时刻不一致 → 这是别的进程，不杀
            $recorded = [string]$state.pid_token
            $current = Get-OpsPidStartToken -ProcessId $pidValue
            if ($recorded -and $current -and $recorded -ne $current) {
                Write-OpsWarn ("$($svc.label)：PID $pidValue 已被系统复用（启动时刻不一致），不执行停止。")
                Remove-OpsRunState -LogsDir $logsDir -Service $svc.name
                continue
            }

            Write-OpsInfo ("$($svc.label)：正在停止（PID $pidValue，先温和后强杀）…")
            $stopped = Stop-OpsProcessTree -ProcessId $pidValue -GracefulSeconds 8 -ForceSeconds 5
            if ($stopped) {
                Write-OpsOk ("$($svc.label)已停止（PID $pidValue）")
            } else {
                Write-OpsWarn ("$($svc.label)停止未确认（PID $pidValue 仍存活，请查看任务管理器）")
            }
            Remove-OpsRunState -LogsDir $logsDir -Service $svc.name
        }

        # ============================ 端口释放核查 ============================
        Write-OpsStep '端口释放核查（8000 / 3000）'
        foreach ($port in @(8000, 3000)) {
            $holders = @(Get-OpsPortPids -Port $port)
            foreach ($hp in $holders) {
                if (Test-OpsPortOwnedByProject -Root $Root -ProcessId $hp -RecordedPids $recordedPids) {
                    Write-OpsWarn ("端口 $port 仍有本项目残留进程（PID $hp），继续清理…")
                    [void](Stop-OpsProcessTree -ProcessId $hp -GracefulSeconds 3 -ForceSeconds 5)
                } else {
                    Write-OpsBad ("端口 $port 被非本项目进程占用（PID $hp）—— 未强杀，请自行确认。")
                    $exitCode = 1
                }
            }
            $left = @(Get-OpsPortPids -Port $port)
            if ($left.Count -eq 0) {
                Write-OpsOk ("端口 $port 已释放")
            } elseif ($exitCode -eq 0) {
                Write-OpsBad ("端口 $port 仍被占用（PID " + ($left -join ', ') + '）')
                $exitCode = 1
            }
        }

        # ============================ 常驻监控收尾 ============================
        $watchState = Read-OpsRunState -LogsDir $logsDir -Service 'watch'
        if ($null -ne $watchState) {
            $watchPid = [int]$watchState.pid
            Write-OpsStep '常驻监控（-Watch）收尾'
            if (Test-OpsPidAlive -ProcessId $watchPid) {
                Write-OpsInfo ("等待监控进程退出（PID $watchPid，最多 15 秒；它收到停止请求后不会重启服务）…")
                $deadline = (Get-Date).AddSeconds(15)
                while ((Get-Date) -lt $deadline) {
                    if (-not (Test-OpsPidAlive -ProcessId $watchPid)) { break }
                    Start-Sleep -Milliseconds 500
                }
                if (Test-OpsPidAlive -ProcessId $watchPid) {
                    Write-OpsWarn '监控进程未自行退出，执行结束。'
                    [void](Stop-OpsProcessTree -ProcessId $watchPid -GracefulSeconds 3 -ForceSeconds 5)
                }
                Write-OpsOk '常驻监控已结束'
            } else {
                Write-OpsOk '常驻监控进程已不在（无需处理）'
            }
            Remove-OpsRunState -LogsDir $logsDir -Service 'watch'
        }

        # ============================ 容器（可选） ============================
        if ($DownContainers) {
            Write-OpsStep '停止容器（docker compose down，保留数据卷）'
            $down = Invoke-OpsCompose -Root $Root -Arguments @('down')
            if ($down.rc -eq 0) {
                Write-OpsOk '容器已停止（db / redis / dailyhot）'
                Write-OpsInfo '数据卷 xhs-agent-pgdata / xhs-agent-redis-data 已保留（本脚本绝不携带卷删除参数）。'
            } else {
                Write-OpsBad 'docker compose down 失败：'
                foreach ($line in @($down.output | Select-Object -Last 5)) { Write-OpsInfo ([string]$line) }
                $exitCode = 1
            }
        } else {
            Write-OpsInfo '容器保留运行（如需一并停止：stop.ps1 -DownContainers；数据卷永不删除）。'
        }

        Write-Host ''
        if ($exitCode -eq 0) {
            Write-OpsOk '停止完成。'
        } else {
            Write-OpsWarn '停止完成，但存在需要人工确认的项（见上方 [X] 行）。'
        }
        exit $exitCode
    } finally {
        Remove-Item -LiteralPath $stopMarker -Force -ErrorAction SilentlyContinue
    }
} catch {
    Write-OpsBad ('停止过程出现意外错误：' + (Get-OpsSafeText -Text $_.Exception.Message))
    exit 1
}
