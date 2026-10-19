#Requires -Version 5.1
<#
.SYNOPSIS
    xhs-agent · 一键启动（本机开发/生产，只绑 127.0.0.1）

.DESCRIPTION
    顺序：环境自检（零副作用）→ Docker 容器（db/redis 必需，dailyhot 容错）
    → 迁移只读核对（-Migrate 才执行升级）→ 前端构建（生产模式、按需）
    → 启动后端 uvicorn + 前端 next → 等待健康检查四项全绿 → 打开浏览器。

    典型用法：
        powershell -ExecutionPolicy Bypass -File scripts\ops\start.ps1
        powershell -ExecutionPolicy Bypass -File scripts\ops\start.ps1 -Dev
        powershell -ExecutionPolicy Bypass -File scripts\ops\start.ps1 -Watch
        双击根目录 start.bat（等价于不带参数的默认用法）

    常用开关：
        -Dev           开发模式：uvicorn --reload + next dev（跳过生产构建）
        -Watch         常驻监控：异常退出按退避自动重启；stop 时不再拉起
        -AutoDocker    Docker 引擎未运行时，尝试启动 Docker Desktop（非管理员）
        -Migrate       数据库落后时执行 alembic upgrade head（有副作用，默认只提示）
        -NoBrowser     不自动打开浏览器（自检/脚本化场景）
        -SkipDailyhot  跳过 dailyhot（热搜刷新暂不可用；用于容错与排障）
        -Root <dir>    指定项目根（默认由脚本位置推断；供自检注入伪根目录）

    退出码约定：
        0 = 已就绪（本次启动成功，或检测到已在运行且健康检查通过）
        1 = 其他错误（端口被外部进程占用 / 前端构建失败 / 意外异常）
        2 = 环境预检未通过（backend\.env / venv / 前端依赖 / Node 缺失）
        3 = Docker 未就绪（未安装 / 引擎未运行 / 容器无法启动）
        4 = 等待健康检查超时
        5 = 数据库迁移未到 head（且未给 -Migrate）

.NOTES
    - 本脚本不代填 / 不修改 backend\.env，不安装依赖，不安装系统软件；
    - 子进程环境已摘除代理变量（避免 localhost 被系统代理劫持）；
    - 运行状态写入 logs\run-backend.json / run-frontend.json（供 stop / status 使用）。
#>
[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$Watch,
    [switch]$AutoDocker,
    [switch]$Migrate,
    [switch]$NoBrowser,
    [switch]$SkipDailyhot,
    [string]$Root = '',
    [int]$HealthTimeoutSec = 120
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ops-common.ps1')
Initialize-OpsConsole

try {
    $Root = Resolve-OpsRoot -ScriptRoot $PSScriptRoot -Override $Root
    Clear-OpsProxyForChild
    $modeName = if ($Dev) { '开发模式 -Dev' } else { '生产模式' }

    Write-Host '=================================================='
    Write-Host (" xhs-agent · 一键启动（" + $modeName + '）')
    Write-Host '=================================================='

    # ============================ 1/7 环境自检（零副作用） ============================
    Write-OpsStep '1/7 环境自检'
    $precheckFailed = $false

    $envFile = Join-Path $Root 'backend\.env'
    if (-not (Test-Path -LiteralPath $envFile)) {
        Write-OpsBad 'backend\.env 缺失（后端唯一配置文件）。'
        Write-OpsInfo '请手动复制并填写（本脚本不代填、不修改）：'
        Write-OpsInfo '    copy backend\.env.example backend\.env'
        $precheckFailed = $true
    } else {
        Write-OpsOk 'backend\.env 就绪'
    }

    $py = Get-OpsPythonExe -Root $Root
    if (-not (Test-Path -LiteralPath $py)) {
        Write-OpsBad 'backend\.venv 虚拟环境缺失（未找到解释器）。'
        Write-OpsInfo '请手动创建并安装依赖（本脚本不自动安装）：'
        Write-OpsInfo '    python -m venv backend\.venv'
        Write-OpsInfo '    backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt'
        $precheckFailed = $true
    } else {
        Write-OpsOk 'backend\.venv 就绪'
    }

    if (-not (Test-Path -LiteralPath (Join-Path $Root 'frontend\node_modules'))) {
        Write-OpsBad '前端依赖缺失（未找到 frontend\node_modules）。'
        Write-OpsInfo '请手动安装（本脚本不自动安装）：cd frontend; npm install'
        $precheckFailed = $true
    } else {
        Write-OpsOk '前端依赖就绪'
    }

    $nodeExe = Get-OpsNodeExe
    $nextJs = Get-OpsNextJs -Root $Root
    if (-not $nodeExe) {
        Write-OpsBad '未找到 node 命令（请安装 Node.js 18+ 并确保在 PATH 中）。'
        $precheckFailed = $true
    } elseif (-not (Test-Path -LiteralPath $nextJs)) {
        Write-OpsBad '未找到 next 可执行文件（frontend\node_modules\next 不完整）。'
        Write-OpsInfo '请重新安装：cd frontend; npm install'
        $precheckFailed = $true
    } else {
        Write-OpsOk 'Node 与 Next 就绪'
    }

    if ($precheckFailed) {
        Write-OpsBad '环境自检未通过：请按上方指引处理后重试（退出码 2）。'
        exit 2
    }

    # ============================ 2/7 Docker ============================
    Write-OpsStep '2/7 Docker 与容器'
    $dockerExe = Get-OpsDockerExe
    if (-not $dockerExe) {
        Write-OpsBad '未检测到 docker 命令（Docker Desktop 未安装或不在 PATH）。'
        Write-OpsInfo '请以管理员运行官方安装脚本：'
        Write-OpsInfo '    powershell -ExecutionPolicy Bypass -File scripts\install-docker.ps1'
        exit 3
    }
    if (-not (Test-OpsDockerEngine)) {
        if ($AutoDocker) {
            Write-OpsWarn 'Docker 引擎未运行：尝试启动 Docker Desktop（-AutoDocker）…'
            [void](Start-OpsDockerDesktop)
            if (-not (Wait-OpsDockerEngine -TimeoutSec 150)) {
                Write-OpsBad '等待 Docker 引擎就绪超时。'
                Write-OpsInfo '请手动启动 Docker Desktop，或运行：scripts\install-docker.ps1'
                exit 3
            }
        } else {
            Write-OpsBad 'Docker 引擎未运行。'
            Write-OpsInfo '请手动启动 Docker Desktop；或重跑本脚本加 -AutoDocker 开关（仅尝试启动）。'
            exit 3
        }
    }
    Write-OpsOk 'Docker 引擎就绪'

    $compose = Invoke-OpsCompose -Root $Root -Arguments @('up', '-d', 'db', 'redis')
    if ($compose.rc -ne 0) {
        Write-OpsBad 'db / redis 容器启动失败：'
        foreach ($line in @($compose.output | Select-Object -Last 6)) { Write-OpsInfo ([string]$line) }
        Write-OpsInfo '请检查镜像是否可用（本机网络受限时可能无法拉取），或运行 scripts\install-docker.ps1。'
        exit 3
    }

    if (Wait-OpsContainerHealthy -Name 'xhs-db' -TimeoutSec 120) {
        Write-OpsOk 'xhs-db（PostgreSQL + pgvector）healthy'
    } else {
        Write-OpsBad 'xhs-db 未在超时内达到 healthy。'
        Write-OpsInfo '排查：docker compose logs db'
        exit 4
    }
    if (Wait-OpsContainerHealthy -Name 'xhs-redis' -TimeoutSec 60) {
        Write-OpsOk 'xhs-redis healthy'
    } else {
        Write-OpsBad 'xhs-redis 未在超时内达到 healthy。'
        Write-OpsInfo '排查：docker compose logs redis'
        exit 4
    }

    if ($SkipDailyhot) {
        Write-OpsWarn '已按 -SkipDailyhot 跳过 dailyhot：热搜刷新暂不可用（不影响核心功能）。'
    } else {
        $dh = Invoke-OpsCompose -Root $Root -Arguments @('up', '-d', 'dailyhot')
        if ($dh.rc -ne 0) {
            Write-OpsWarn 'dailyhot 启动失败（常见原因：镜像不可用 / 网络受限）。'
            Write-OpsWarn '热搜刷新暂不可用；核心服务不受影响，继续启动。'
        } elseif (Wait-OpsContainerHealthy -Name 'xhs-dailyhot' -TimeoutSec 90) {
            Write-OpsOk 'xhs-dailyhot healthy'
        } else {
            Write-OpsWarn 'dailyhot 未在超时内达到 healthy（热搜刷新暂不可用；核心服务不受影响）。'
        }
    }

    # ============================ 3/7 迁移只读核对 ============================
    Write-OpsStep '3/7 数据库迁移核对（只读）'
    $backendDir = Join-Path $Root 'backend'
    $cur = Invoke-OpsCapture -Exe $py -Arguments @('-m', 'alembic', 'current') -WorkingDirectory $backendDir
    $currentOut = ($cur.output -join "`n")
    $currentRc = $cur.rc
    $atHead = ($currentRc -eq 0) -and ($currentOut -match '\(head\)')
    if ($atHead) {
        Write-OpsOk 'alembic current = head'
    } elseif ($Migrate) {
        Write-OpsWarn '数据库未到 head：按 -Migrate 执行 alembic upgrade head …'
        $up = Invoke-OpsCapture -Exe $py -Arguments @('-m', 'alembic', 'upgrade', 'head') -WorkingDirectory $backendDir
        if ($up.rc -ne 0) {
            Write-OpsBad ('alembic upgrade head 执行失败（退出码 ' + $up.rc + '）。')
            foreach ($line in @($up.output | Select-Object -Last 6)) { Write-OpsInfo ([string]$line) }
            exit 5
        }
        $recheck = Invoke-OpsCapture -Exe $py -Arguments @('-m', 'alembic', 'current') -WorkingDirectory $backendDir
        if ((($recheck.output -join "`n") -match '\(head\)')) {
            Write-OpsOk '迁移已升级到 head'
        } else {
            Write-OpsBad '升级后仍未到 head，请手动排查。'
            exit 5
        }
    } else {
        Write-OpsBad '数据库迁移未到 head（当前库结构可能落后于代码）。'
        Write-OpsInfo '请运行（有副作用，本脚本默认不自动执行）：'
        Write-OpsInfo '    重跑 start.ps1 加 -Migrate 开关，或手动：'
        Write-OpsInfo '    cd backend; .venv\Scripts\python.exe -m alembic upgrade head'
        exit 5
    }

    # ============================ 4/7 已在运行探测（幂等） ============================
    Write-OpsStep '4/7 检查是否已在运行'
    $logsDir = Get-OpsLogsDir -Root $Root

    function Get-ServiceRuntime([string]$Service, [int]$Port) {
        $state = Read-OpsRunState -LogsDir $logsDir -Service $Service
        if ($null -eq $state) { return @{ status = 'none'; state = $null; port = $Port } }
        $alive = Test-OpsPidAlive -ProcessId ([int]$state.pid)
        if (-not $alive) { return @{ status = 'stale'; state = $state; port = $Port } }
        $listening = (@(Get-OpsPortPids -Port $Port)).Count -gt 0
        if ($listening) { return @{ status = 'running'; state = $state; port = $Port } }
        $ageSec = 999999
        try {
            $ageSec = ((Get-Date).ToUniversalTime() - ([datetime]$state.started_at).ToUniversalTime()).TotalSeconds
        } catch { }
        if ($ageSec -le 120) { return @{ status = 'starting'; state = $state; port = $Port } }
        return @{ status = 'unhealthy'; state = $state; port = $Port }
    }

    $beRun = Get-ServiceRuntime 'backend' 8000
    $feRun = Get-ServiceRuntime 'frontend' 3000
    # 端口归属判定用：记录过的进程（含陈旧记录），用于识别「我们的子进程持有端口」
    $recordedPids = @()
    if ($beRun.state) { $recordedPids += [int]$beRun.state.pid }
    if ($feRun.state) { $recordedPids += [int]$feRun.state.pid }

    if ($beRun.status -eq 'stale') { Remove-OpsRunState -LogsDir $logsDir -Service 'backend'; $beRun = @{ status = 'none'; state = $null; port = 8000 } }
    if ($feRun.status -eq 'stale') { Remove-OpsRunState -LogsDir $logsDir -Service 'frontend'; $feRun = @{ status = 'none'; state = $null; port = 3000 } }

    # 外部进程占用端口（非本脚本记录）→ 明确拒绝，不静默强杀
    foreach ($item in @(@{ svc = '后端'; port = 8000; run = $beRun }, @{ svc = '前端'; port = 3000; run = $feRun })) {
        if ($item.run.status -eq 'none') {
            $holders = @(Get-OpsPortPids -Port $item.port)
            if ($holders.Count -gt 0) {
                $ownerOk = $false
                foreach ($hp in $holders) {
                    if (Test-OpsPortOwnedByProject -Root $Root -ProcessId $hp -RecordedPids $recordedPids) { $ownerOk = $true }
                }
                if (-not $ownerOk) {
                    Write-OpsBad ("$($item.svc)端口 $($item.port) 被其他进程占用（PID " + ($holders -join ', ') + '）。')
                    Write-OpsInfo '为避免误杀，请先手动停止占用进程（可能是手动启动的旧服务），再重试。'
                    exit 1
                }
                # 属于本项目但无运行记录：视为「进程级已在运行（旧记录已丢失）」
                Write-OpsWarn ("$($item.svc)端口 $($item.port) 已被本项目进程占用（PID " + ($holders -join ', ') + '，无运行记录）。')
                $item.run.status = 'running-foreign-record'
            }
        }
    }

    if ($beRun.status -eq 'running' -and $feRun.status -eq 'running') {
        Write-OpsWarn '检测到后端与前端均已在运行（本次不重复拉起）。'
        if ($Watch) {
            Write-OpsWarn '-Watch 只监控本次启动的进程；如需常驻监控：先运行 stop 再以 -Watch 启动。'
        }
        Write-Host ''
        Write-Host ' 正在复核健康检查…'
        $hcr = Invoke-OpsCapture -Exe $py -Arguments @((Join-Path $Root 'scripts\healthcheck.py')) -WorkingDirectory $Root
        $hc = ($hcr.output -join "`n")
        $hcRc = $hcr.rc
        Write-Host $hc.TrimEnd()
        if ($hcRc -eq 0) {
            Write-OpsOk '已在运行且健康检查全绿。'
            if (-not $NoBrowser) { Start-Process 'http://localhost:3000' | Out-Null }
            exit 0
        } else {
            Write-OpsBad '已在运行，但健康检查未通过（请看上方明细与 logs\ 下最新日志）。'
            exit 4
        }
    }

    if ($beRun.status -eq 'unhealthy' -or $feRun.status -eq 'unhealthy') {
        Write-OpsBad '检测到残留进程：记录中的进程仍在，但端口未监听（可能启动失败或已挂起）。'
        Write-OpsInfo '请先运行 stop（scripts\ops\stop.ps1 或双击 stop.bat），再重试。'
        exit 1
    }

    # ============================ 5/7 前端构建（生产、按需） ============================
    Write-OpsStep '5/7 前端构建'
    if ($Dev) {
        Write-OpsOk '开发模式（-Dev）：跳过生产构建（next dev 实时编译）'
    } else {
        $need = Test-OpsFrontendBuildNeeded -Root $Root
        if ($need.needed) {
            Write-OpsInfo ("需要构建：" + $need.reason + '（首次构建约 1~2 分钟）…')
            $buildLog = Join-Path $logsDir ("frontend-build-" + (Get-OpsTimestamp) + '.log')
            $b = Invoke-OpsCapture -Exe $nodeExe -Arguments @($nextJs, 'build') -WorkingDirectory (Join-Path $Root 'frontend')
            $buildRc = $b.rc
            try { $b.output | Set-Content -LiteralPath $buildLog -Encoding UTF8 } catch { }
            if ($buildRc -ne 0) {
                Write-OpsBad ('前端构建失败（退出码 ' + $buildRc + '）。')
                Write-OpsInfo ('构建日志：' + (Get-OpsRelPath -Root $Root -Path $buildLog))
                exit 1
            }
            Write-OpsOk '前端构建完成'
        } else {
            Write-OpsOk ($need.reason + '，跳过构建')
        }
    }

    # ============================ 6/7 启动进程 ============================
    Write-OpsStep '6/7 启动后端与前端'
    $spawnBackend = $null
    $spawnFrontend = $null

    if ($beRun.status -eq 'running' -or $beRun.status -eq 'starting' -or $beRun.status -eq 'running-foreign-record') {
        Write-OpsWarn ('后端已在运行（PID ' + $beRun.state.pid + '），跳过启动')
    } else {
        $spawnBackend = Start-OpsService -Root $Root -Which backend -Dev:$Dev
        if (-not $spawnBackend.ok) {
            Write-OpsBad '后端进程启动失败（解释器缺失？）。'
            exit 1
        }
        $beState = @{
            service    = 'backend'
            pid        = $spawnBackend.pid
            port       = 8000
            mode       = $(if ($Dev) { 'dev' } else { 'prod' })
            command    = $spawnBackend.command
            log        = (Get-OpsRelPath -Root $Root -Path $spawnBackend.log)
            log_err    = (Get-OpsRelPath -Root $Root -Path $spawnBackend.log_err)
            started_at = (Get-Date).ToString('o')
            pid_token  = (Get-OpsPidStartToken -ProcessId $spawnBackend.pid)
        }
        Write-OpsRunState -LogsDir $logsDir -Service 'backend' -State $beState
        Write-OpsOk ('后端已启动（PID ' + $spawnBackend.pid + '，' + $spawnBackend.command + '）')
        Write-OpsInfo ('日志：' + $beState.log)
    }

    if ($feRun.status -eq 'running' -or $feRun.status -eq 'starting' -or $feRun.status -eq 'running-foreign-record') {
        Write-OpsWarn ('前端已在运行（PID ' + $feRun.state.pid + '），跳过启动')
    } else {
        $spawnFrontend = Start-OpsService -Root $Root -Which frontend -Dev:$Dev
        if (-not $spawnFrontend.ok) {
            Write-OpsBad '前端进程启动失败（node / next 缺失？）。'
            exit 1
        }
        $feState = @{
            service    = 'frontend'
            pid        = $spawnFrontend.pid
            port       = 3000
            mode       = $(if ($Dev) { 'dev' } else { 'prod' })
            command    = $spawnFrontend.command
            log        = (Get-OpsRelPath -Root $Root -Path $spawnFrontend.log)
            log_err    = (Get-OpsRelPath -Root $Root -Path $spawnFrontend.log_err)
            started_at = (Get-Date).ToString('o')
            pid_token  = (Get-OpsPidStartToken -ProcessId $spawnFrontend.pid)
        }
        Write-OpsRunState -LogsDir $logsDir -Service 'frontend' -State $feState
        Write-OpsOk ('前端已启动（PID ' + $spawnFrontend.pid + '，' + $spawnFrontend.command + '）')
        Write-OpsInfo ('日志：' + $feState.log)
    }

    # ============================ 7/7 等待健康检查 ============================
    Write-OpsStep '7/7 等待服务就绪（健康检查）'
    $deadline = (Get-Date).AddSeconds($HealthTimeoutSec)
    $passed = $false
    $lastOut = ''
    $waited = 0
    while ((Get-Date) -lt $deadline) {
        $hcr = Invoke-OpsCapture -Exe $py -Arguments @((Join-Path $Root 'scripts\healthcheck.py')) -WorkingDirectory $Root
        $lastOut = ($hcr.output -join "`n")
        $hcRc = $hcr.rc
        if ($hcRc -eq 0) { $passed = $true; break }
        Start-Sleep -Seconds 5
        $waited += 5
        if ($waited % 15 -eq 0) { Write-OpsInfo ('仍在等待（已 ' + $waited + ' 秒 / 上限 ' + $HealthTimeoutSec + ' 秒）…') }
    }

    if (-not $passed) {
        Write-OpsBad '等待健康检查通过超时。最近一次探活结果：'
        Write-Host $lastOut.TrimEnd()
        Write-OpsInfo '排查建议：'
        Write-OpsInfo '  1) 查看后端日志：logs\ 下最新的 backend-*.log / backend-*.err.log'
        Write-OpsInfo '  2) 查看前端日志：logs\ 下最新的 frontend-*.log / frontend-*.err.log'
        Write-OpsInfo '  3) 确认 backend\.env 配置正确（数据库口令等）'
        Write-OpsInfo '  4) 停止后重试：双击 stop.bat（或 scripts\ops\stop.ps1），再重新 start'
        exit 4
    }

    Write-Host $lastOut.TrimEnd()
    Write-OpsOk '健康检查全部通过（前端可达 + 后端存活 + database/pgvector/redis 均为 true）'

    # ============================ -Watch 常驻监控 ============================
    if ($Watch) {
        Write-OpsStep '进入常驻监控（-Watch）'
        Write-OpsInfo '异常退出 → 按退避自动重启（2/5/10/20/30/60 秒封顶）；手动 stop 不再拉起。'
        Write-OpsInfo '结束方式：关闭本窗口（Ctrl+C）或运行 stop（stop 会同时结束本监控）。'

        $watchLog = Join-Path $logsDir ('watch-' + (Get-OpsTimestamp) + '.log')
        $stopMarker = Join-Path $logsDir 'stop.request'
        Remove-Item -LiteralPath $stopMarker -Force -ErrorAction SilentlyContinue
        Write-OpsRunState -LogsDir $logsDir -Service 'watch' -State @{
            pid        = [int]$PID
            started_at = (Get-Date).ToString('o')
            log        = (Get-OpsRelPath -Root $Root -Path $watchLog)
        }

        $procMap = @{}
        if ($spawnBackend) { $procMap['backend'] = $spawnBackend.proc }
        if ($spawnFrontend) { $procMap['frontend'] = $spawnFrontend.proc }
        $restarts = @{ backend = 0; frontend = 0 }
        $backoffs = @(2, 5, 10, 20, 30, 60)

        function Write-WatchLog([string]$line) {
            $stamp = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
            try { ($stamp + ' ' + $line) | Add-Content -LiteralPath $watchLog -Encoding UTF8 } catch { }
        }

        Write-WatchLog '常驻监控启动'
        $lastBeat = Get-Date
        $loopExit = $false
        while (-not $loopExit) {
            Start-Sleep -Seconds 5
            if (Test-Path -LiteralPath $stopMarker) { break }

            foreach ($svc in @('backend', 'frontend')) {
                if (Test-Path -LiteralPath $stopMarker) { $loopExit = $true; break }
                $state = Read-OpsRunState -LogsDir $logsDir -Service $svc
                if ($null -eq $state) { continue }
                if (Test-OpsPidAlive -ProcessId ([int]$state.pid)) { continue }

                $reason = '进程已退出'
                try {
                    $po = $procMap[$svc]
                    if ($po) {
                        $po.Refresh()
                        if ($po.HasExited) {
                            $code = ''
                            try { $code = [string]$po.ExitCode } catch { }
                            if ($code) { $reason = '进程已退出（退出码 ' + $code + '）' }
                        }
                    }
                } catch { }

                $restarts[$svc] = $restarts[$svc] + 1
                $idx = [Math]::Min($restarts[$svc] - 1, $backoffs.Count - 1)
                $waitSec = $backoffs[$idx]
                $line = ($svc + '：' + $reason + '；第 ' + $restarts[$svc] + ' 次重启，等待 ' + $waitSec + ' 秒…')
                Write-WatchLog $line
                Write-OpsWarn $line

                $until = (Get-Date).AddSeconds($waitSec)
                while ((Get-Date) -lt $until) {
                    if (Test-Path -LiteralPath $stopMarker) { $loopExit = $true; break }
                    Start-Sleep -Milliseconds 500
                }
                if ($loopExit) { break }

                Clear-OpsProxyForChild
                $spawn = Start-OpsService -Root $Root -Which $svc -Dev:$Dev -Suffix ('r' + $restarts[$svc])
                if ($spawn.ok) {
                    $procMap[$svc] = $spawn.proc
                    $port = 8000
                    if ($svc -eq 'frontend') { $port = 3000 }
                    Write-OpsRunState -LogsDir $logsDir -Service $svc -State @{
                        service       = $svc
                        pid           = $spawn.pid
                        port          = $port
                        mode          = $(if ($Dev) { 'dev' } else { 'prod' })
                        command       = $spawn.command
                        log           = (Get-OpsRelPath -Root $Root -Path $spawn.log)
                        log_err       = (Get-OpsRelPath -Root $Root -Path $spawn.log_err)
                        started_at    = (Get-Date).ToString('o')
                        pid_token     = (Get-OpsPidStartToken -ProcessId $spawn.pid)
                        restart_count = $restarts[$svc]
                    }
                    $okLine = ($svc + ' 已重启（PID ' + $spawn.pid + '）')
                    Write-WatchLog $okLine
                    Write-OpsOk $okLine
                } else {
                    $failLine = ($svc + ' 重启失败（依赖缺失？稍后继续重试）')
                    Write-WatchLog $failLine
                    Write-OpsBad $failLine
                }
            }

            if (((Get-Date) - $lastBeat).TotalSeconds -ge 60) {
                $lastBeat = Get-Date
                $bePid = '-'
                $fePid = '-'
                $s1 = Read-OpsRunState -LogsDir $logsDir -Service 'backend'
                if ($s1) { $bePid = [string]$s1.pid }
                $s2 = Read-OpsRunState -LogsDir $logsDir -Service 'frontend'
                if ($s2) { $fePid = [string]$s2.pid }
                Write-Host ('  [心跳] ' + (Get-Date -Format 'HH:mm:ss') + ' 后端 PID ' + $bePid +
                    '（重启 ' + $restarts['backend'] + ' 次）｜前端 PID ' + $fePid +
                    '（重启 ' + $restarts['frontend'] + ' 次）') -ForegroundColor DarkGray
            }
        }

        Write-WatchLog '常驻监控结束'
        Remove-OpsRunState -LogsDir $logsDir -Service 'watch'
        Remove-Item -LiteralPath $stopMarker -Force -ErrorAction SilentlyContinue
        Write-OpsInfo '常驻监控已结束（服务进程仍在运行；如需全部停止：双击 stop.bat）。'
        exit 0
    }

    # ============================ 完成 ============================
    Write-Host ''
    Write-Host '=================================================='
    Write-OpsOk 'xhs-agent 已启动'
    Write-Host '  后端：http://localhost:8000'
    Write-Host '  前端：http://localhost:3000'
    Write-Host '  停止：双击 stop.bat（或运行 scripts\ops\stop.ps1）'
    Write-Host '  状态：运行 scripts\ops\status.ps1 查看容器/进程/健康'
    Write-Host '=================================================='

    if (-not $NoBrowser) {
        Start-Process 'http://localhost:3000' | Out-Null
        Write-OpsInfo '已打开浏览器 http://localhost:3000'
    }
    exit 0
} catch {
    Write-OpsBad ('启动过程出现意外错误：' + (Get-OpsSafeText -Text $_.Exception.Message))
    Write-OpsInfo ('位置：脚本第 ' + $_.InvocationInfo.ScriptLineNumber + ' 行（详见上方消息）')
    exit 1
}
