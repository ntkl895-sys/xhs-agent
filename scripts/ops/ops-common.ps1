# ============================================================================
# xhs-agent · 运维脚本共享函数库（第 20 步）
#
# 本文件【只定义函数与常量】，不执行任何实际操作；
# 由 scripts/ops/ 下的各入口脚本 dot-source（. "$PSScriptRoot\ops-common.ps1"）。
#
# 设计约定（全项目一致）：
#   - 纯 Windows 内置能力：PowerShell + 计划任务（Docker Desktop 由官方安装包管理）；
#   - 所有面向用户的输出【只用项目相对路径】（如 logs\backend-x.log），
#     绝不回显绝对路径、API key、.env 内容；
#   - 子进程（uvicorn / next）继承「清洗过的环境」：摘除代理变量 + NO_PROXY 兜底，
#     避免本机回环请求被系统代理劫持（第 9/10 步起全项目统一做法）；
#   - 只读取、不代填：.env / 密钥一律不自动创建、不自动修改。
# ============================================================================

$script:OpsCommonLoaded = $true

# 任务计划程序里的注册位置（卸载脚本按同样的「任务名 + 路径」查找）
$script:OpsTaskFolderName = 'xhs-agent'

# 本机回环地址（健康检查 / 等待就绪只用 127.0.0.1 或 localhost，不触达外部网络）
$script:OpsLocalHost = '127.0.0.1'


# ============================ 输出辅助 ============================
function Write-OpsStep([string]$text) {
    Write-Host ''
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Write-OpsOk([string]$text) {
    Write-Host "  [OK] $text" -ForegroundColor Green
}

function Write-OpsWarn([string]$text) {
    Write-Host "  [!]  $text" -ForegroundColor Yellow
}

function Write-OpsBad([string]$text) {
    Write-Host "  [X]  $text" -ForegroundColor Red
}

function Write-OpsInfo([string]$text) {
    Write-Host "       $text"
}


# ============================ 基础工具 ============================

function Initialize-OpsConsole {
    # 让本脚本在管道/重定向场景下也稳定输出 UTF-8（5.1 默认 OEM 代码页会乱码）
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
    try { $script:OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
}

function Resolve-OpsRoot {
    <#
    .SYNOPSIS 解析项目根目录。
    .DESCRIPTION
        默认由脚本位置推断（scripts\ops\ 的上两级）。
        校验根目录下必须存在 docker-compose.yml / backend / frontend，
        否则抛错（防止被复制到别处后误操作）。
        -Override 供自检注入伪根目录使用。
    #>
    param(
        [string]$ScriptRoot = '',
        [string]$Override = ''
    )
    $root = if ($Override) { $Override } else {
        [System.IO.Path]::GetFullPath((Join-Path $ScriptRoot '..\..'))
    }
    if (-not (Test-Path -LiteralPath $root)) {
        throw "项目根目录不存在：$root"
    }
    try { $root = (Resolve-Path -LiteralPath $root).Path } catch { }
    foreach ($marker in @('docker-compose.yml', 'backend', 'frontend')) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $marker))) {
            throw "目标目录不像 xhs-agent 项目根（缺少 $marker）：$root"
        }
    }
    return $root
}

function Get-OpsRelPath {
    <# 绝对路径 → 项目相对路径（仅用于展示；绝不在输出中回显绝对路径）。 #>
    param(
        [string]$Root,
        [string]$Path
    )
    try {
        $fullRoot = [System.IO.Path]::GetFullPath($Root)
        $fullPath = [System.IO.Path]::GetFullPath($Path)
        if ($fullPath.StartsWith($fullRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            $rel = $fullPath.Substring($fullRoot.Length).TrimStart('\', '/')
            if ($rel) { return $rel }
            return '.'
        }
    } catch { }
    return [System.IO.Path]::GetFileName($Path)
}

function Get-OpsSafeText {
    <#
    .SYNOPSIS 把任意文本里的「绝对路径 / 疑似密钥」替换为占位符。
    .DESCRIPTION
        所有面向用户的异常文本统一经此过滤后再输出：
        - Windows 盘符路径（C:\...）→「[路径已隐藏]」；
        - /Users、/home 等 POSIX 用户路径 →「[路径已隐藏]」；
        - sk- 开头的疑似密钥片段 →「[已隐藏]」。
    #>
    param([string]$Text)
    if (-not $Text) { return '' }
    $t = [string]$Text
    $t = [regex]::Replace($t, '[A-Za-z]:\\[^\s"''<>|]*', '[路径已隐藏]')
    $t = [regex]::Replace($t, '(?<![\w:/])/(?:Users|home|tmp|var|opt|root)[^\s"''<>|]*', '[路径已隐藏]')
    $t = [regex]::Replace($t, 'sk-[A-Za-z0-9_\-]{6,}', '[已隐藏]')
    return $t
}

function Test-OpsPidAlive {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return $false }
    try {
        $p = Get-Process -Id $ProcessId -ErrorAction Stop
        return (-not $p.HasExited)
    } catch {
        return $false
    }
}

function Get-OpsPidStartToken {
    <# 进程启动时刻（epoch 秒，字符串）；取不到返回 ''。
       用途：stop 时校验 PID 未被系统复用（启动时刻吻合才认为是我们的进程）。 #>
    param([int]$ProcessId)
    try {
        $p = Get-Process -Id $ProcessId -ErrorAction Stop
        $epoch = [int][double]::Parse(
            ((Get-Date $p.StartTime.ToUniversalTime()) - (Get-Date '1970-01-01T00:00:00Z')).TotalSeconds,
            [System.Globalization.CultureInfo]::InvariantCulture
        )
        return [string]$epoch
    } catch {
        return ''
    }
}

function Get-OpsPortPids {
    <# 监听某端口的进程 id 列表。 #>
    param([int]$Port)
    $pids = @()
    try {
        $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        foreach ($c in $conns) { if ($c.OwningProcess) { $pids += [int]$c.OwningProcess } }
    } catch {
        # 兜底：netstat 解析（LOCAL_ADDRESS:PORT ... LISTENING PID）
        try {
            $r = Invoke-OpsCapture -Exe 'netstat' -Arguments @('-ano', '-p', 'TCP')
            foreach ($line in $r.output) {
                if ($line -match ("[:\.]" + $Port + "\s") -and $line -match 'LISTENING') {
                    $parts = ($line -split '\s+') | Where-Object { $_ -ne '' }
                    if ($parts.Count -ge 5) { $pids += [int]$parts[-1] }
                }
            }
        } catch { }
    }
    return @($pids | Sort-Object -Unique)
}


# ============================ 代理环境清洗 ============================

function Clear-OpsProxyForChild {
    <#
    .SYNOPSIS 为子进程清洗代理环境变量。
    .DESCRIPTION
        本项目实测：系统级 HTTP(S)_PROXY 会让 uvicorn / next 对 localhost 的
        请求被代理劫持（表现为前端「共 0 条」+ 503）。统一做法：
        摘除 6 个代理变量 + 设置 NO_PROXY 兜底，让本机回环流量直连。
        ⚠️ 只影响本脚本进程与其子进程，不改动系统设置。
    #>
    foreach ($name in @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')) {
        try { Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue } catch { }
    }
    $env:NO_PROXY = 'localhost,127.0.0.1,::1'
    $env:no_proxy = 'localhost,127.0.0.1,::1'
}


# ============================ Docker ============================

function Invoke-OpsCapture {
    <#
    .SYNOPSIS 运行原生命令并捕获「stdout + stderr 合并文本」与退出码（绝不抛异常）。
    .DESCRIPTION
        Windows PowerShell 5.1 的已知坑：在 $ErrorActionPreference='Stop' 下，
        对原生命令重定向 stderr（无论 2>$null 还是 2>&1）都会把 stderr 输出
        误解为终止性错误。统一在本函数内**就地临时**改为 Continue，
        捕获全部输出后恢复原设置 —— 调用方无需关心该细节。
    .OUTPUTS @{ rc; output } —— output 为字符串数组。
    #>
    param(
        [string]$Exe,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = ''
    )
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $pushed = $false
    try {
        if ($WorkingDirectory) {
            Push-Location -LiteralPath $WorkingDirectory
            $pushed = $true
        }
        $out = & $Exe @Arguments 2>&1
        $rc = $LASTEXITCODE
    } catch {
        $out = @('调用失败：' + (Get-OpsSafeText -Text $_.Exception.Message))
        $rc = 1
    } finally {
        if ($pushed) { Pop-Location }
        $ErrorActionPreference = $old
    }
    return @{ rc = $rc; output = @($out | ForEach-Object { [string]$_ }) }
}

function Get-OpsDockerExe {
    $c = Get-Command docker -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Docker\Docker\resources\bin\docker.exe'),
        (Join-Path $env:LOCALAPPDATA 'Docker\Docker\resources\bin\docker.exe')
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    }
    return $null
}

function Test-OpsDockerEngine {
    $exe = Get-OpsDockerExe
    if (-not $exe) { return $false }
    $r = Invoke-OpsCapture -Exe $exe -Arguments @('info')
    return ($r.rc -eq 0)
}

function Start-OpsDockerDesktop {
    <# 只在「非管理员下尝试」启动 Docker Desktop（官方安装器管理其更新）。 #>
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Docker\Docker\Docker Desktop.exe'),
        (Join-Path $env:LOCALAPPDATA 'Docker\Docker\Docker Desktop.exe')
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path -LiteralPath $p)) {
            try {
                Start-Process -FilePath $p | Out-Null
                return $true
            } catch { }
        }
    }
    return $false
}

function Wait-OpsDockerEngine {
    param([int]$TimeoutSec = 120)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-OpsDockerEngine) { return $true }
        Start-Sleep -Seconds 5
    }
    return (Test-OpsDockerEngine)
}

function Invoke-OpsCompose {
    <#
    .SYNOPSIS 在项目根执行 docker compose（统一入口，便于测试与审计）。
    .OUTPUTS @{ rc; output } —— output 为字符串数组（原样，不做编码转换）。
    #>
    param(
        [string]$Root,
        [string[]]$Arguments
    )
    $exe = Get-OpsDockerExe
    if (-not $exe) { return @{ rc = 127; output = @('未找到 docker 命令') } }
    $composeArgs = @('compose') + $Arguments
    return Invoke-OpsCapture -Exe $exe -Arguments $composeArgs -WorkingDirectory $Root
}

function Get-OpsContainerHealth {
    <#
    .SYNOPSIS 某容器当前状态：healthy / unhealthy / starting / running / exited / missing。
    #>
    param([string]$Name)
    $exe = Get-OpsDockerExe
    if (-not $exe) { return 'missing' }
    $r1 = Invoke-OpsCapture -Exe $exe -Arguments @('inspect', '--format', '{{.State.Status}}', $Name)
    if ($r1.rc -ne 0 -or $r1.output.Count -eq 0) { return 'missing' }
    $state = [string]$r1.output[0]
    if ($state -eq 'running') {
        $r2 = Invoke-OpsCapture -Exe $exe -Arguments @('inspect', '--format', '{{if .State.Health}}{{.State.Health.Status}}{{end}}', $Name)
        if ($r2.rc -eq 0 -and $r2.output.Count -gt 0 -and ([string]$r2.output[0]).Trim()) {
            return ([string]$r2.output[0]).Trim()  # healthy / unhealthy / starting
        }
        return 'running'  # 无 healthcheck 的容器（本项目三个都有，兜底）
    }
    return $state
}

function Wait-OpsContainerHealthy {
    <# 轮询容器到 healthy（无 healthcheck 时接受 running）；超时返回 $false。 #>
    param(
        [string]$Name,
        [int]$TimeoutSec = 120
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $state = Get-OpsContainerHealth -Name $Name
        if ($state -eq 'healthy' -or $state -eq 'running') { return $true }
        if ($state -eq 'missing') { return $false }
        Start-Sleep -Seconds 2
    }
    $final = Get-OpsContainerHealth -Name $Name
    return ($final -eq 'healthy' -or $final -eq 'running')
}


# ============================ HTTP 探活（绕过代理） ============================

function Wait-OpsHttpHealthy {
    <#
    .SYNOPSIS 轮询 URL 直到 HTTP 可达（2xx~3xx）；超时返回 $false，绝不抛异常。
    .DESCRIPTION
        使用显式 Proxy=$null 的 WebClient，绕过系统代理（本机回环探活）。
        该函数同时被自检用作「不可达 → 优雅返回 $false」的受控测试目标。
    #>
    param(
        [string]$Url,
        [int]$TimeoutSec = 30,
        [int]$IntervalSec = 2,
        [int]$HttpTimeoutMs = 3000
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $ok = $false
        try {
            $wc = New-Object System.Net.WebClient
            $wc.Proxy = $null
            $wc.Headers.Add('User-Agent', 'xhs-agent-ops')
            $task = $wc.DownloadStringTaskAsync($Url)
            if ($task.Wait($HttpTimeoutMs)) {
                $ok = $true
            }
            $wc.Dispose()
        } catch {
            $ok = $false
        }
        if ($ok) { return $true }
        Start-Sleep -Seconds $IntervalSec
    }
    return $false
}


# ============================ 运行状态（run-*.json） ============================

function Get-OpsLogsDir {
    param([string]$Root)
    $dir = Join-Path $Root 'logs'
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }
    return $dir
}

function Read-OpsRunState {
    <# 读取 logs\run-<service>.json；不存在/解析失败返回 $null。 #>
    param(
        [string]$LogsDir,
        [string]$Service
    )
    $path = Join-Path $LogsDir ("run-" + $Service + ".json")
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try {
        $raw = Get-Content -LiteralPath $path -Raw -Encoding UTF8
        return ($raw | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Write-OpsRunState {
    param(
        [string]$LogsDir,
        [string]$Service,
        $State
    )
    $path = Join-Path $LogsDir ("run-" + $Service + ".json")
    try {
        ($State | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $path -Encoding UTF8
    } catch {
        Write-OpsWarn ('写入运行状态失败（不影响服务）：' + (Get-OpsSafeText -Text $_.Exception.Message))
    }
}

function Remove-OpsRunState {
    param(
        [string]$LogsDir,
        [string]$Service
    )
    $path = Join-Path $LogsDir ("run-" + $Service + ".json")
    try { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue } catch { }
}

function Get-OpsTimestamp {
    return (Get-Date -Format 'yyyyMMdd-HHmmss')
}


# ============================ 进程树停止 ============================

function Stop-OpsProcessTree {
    <#
    .SYNOPSIS 优雅停止进程树：先发关闭信号，等待；超时再强杀。
    .DESCRIPTION
        Windows 对控制台进程没有真正的 SIGTERM：
        步骤 1：taskkill /PID <id> /T（不带 /F，先尝试「温和」结束；未强杀）；
        步骤 2：等待 -GracefulSeconds 秒，仍未退出 → taskkill /PID <id> /T /F 强杀；
        步骤 3：再等 -ForceSeconds 秒，返回最终是否已退出。
        /T 会一并覆盖子进程（uvicorn --reload 的 reloader+worker、next 的子进程）。
    .OUTPUTS $true = 进程（树）已结束；$false = 仍未结束。
    #>
    param(
        [int]$ProcessId,
        [int]$GracefulSeconds = 8,
        [int]$ForceSeconds = 5
    )
    if (-not (Test-OpsPidAlive -ProcessId $ProcessId)) { return $true }

    [void](Invoke-OpsCapture -Exe 'taskkill' -Arguments @('/PID', [string]$ProcessId, '/T'))
    $deadline = (Get-Date).AddSeconds($GracefulSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-OpsPidAlive -ProcessId $ProcessId)) { return $true }
        Start-Sleep -Milliseconds 500
    }

    [void](Invoke-OpsCapture -Exe 'taskkill' -Arguments @('/PID', [string]$ProcessId, '/T', '/F'))
    $deadline2 = (Get-Date).AddSeconds($ForceSeconds)
    while ((Get-Date) -lt $deadline2) {
        if (-not (Test-OpsPidAlive -ProcessId $ProcessId)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return (-not (Test-OpsPidAlive -ProcessId $ProcessId))
}

function Get-OpsProcessAncestors {
    <# 某进程的祖先链（含自身；最多向上 8 层）。用于「端口持有者是否属于我们的进程树」判定。 #>
    param(
        [int]$ProcessId,
        [int]$MaxDepth = 8
    )
    $chain = @([int]$ProcessId)
    $cur = [int]$ProcessId
    for ($i = 0; $i -lt $MaxDepth; $i++) {
        $p = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $cur) -ErrorAction SilentlyContinue
        if (-not $p) { break }
        $parentId = [int]$p.ParentProcessId
        if ($parentId -le 0) { break }
        $chain += $parentId
        $cur = $parentId
    }
    return $chain
}

function Test-OpsPortOwnedByProject {
    <#
    .SYNOPSIS 端口持有者是否属于本项目可控进程。
    .DESCRIPTION
        依次判定（任一满足即为「属于」）：
        ① 就是运行记录中的进程（RecordedPids）；
        ② 是记录进程的后代（祖先链包含任一记录 PID）——
           uvicorn / next 都会派生子进程持有端口（reload 的 worker、next 的 server 子进程）；
        ③ 可执行文件位于项目目录内（backend\.venv 的 python 等）。
        ③ 对全局安装的 node 不适用（其 exe 在项目外），因此①②是主判据。
    #>
    param(
        [string]$Root,
        [int]$ProcessId,
        [int[]]$RecordedPids = @()
    )
    if ($ProcessId -le 0) { return $false }
    if ($RecordedPids -contains $ProcessId) { return $true }
    if ($RecordedPids.Count -gt 0) {
        $chain = @(Get-OpsProcessAncestors -ProcessId $ProcessId)
        foreach ($rp in $RecordedPids) {
            if ($chain -contains [int]$rp) { return $true }
        }
    }
    try {
        $p = Get-Process -Id $ProcessId -ErrorAction Stop
        $path = $p.Path
        if ($path) {
            $norm = $path.ToLowerInvariant()
            $rootNorm = ([System.IO.Path]::GetFullPath($Root)).ToLowerInvariant()
            return $norm.StartsWith($rootNorm)
        }
    } catch { }
    return $false
}


# ============================ 服务启动（uvicorn / next） ============================

function Get-OpsPythonExe {
    param([string]$Root)
    return (Join-Path $Root 'backend\.venv\Scripts\python.exe')
}

function Get-OpsNodeExe {
    $c = Get-Command node -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return $null
}

function Get-OpsNextJs {
    param([string]$Root)
    return (Join-Path $Root 'frontend\node_modules\next\dist\bin\next')
}

function Start-OpsService {
    <#
    .SYNOPSIS 启动后端或前端进程（隐藏窗口 + stdout/stderr 重定向到 logs\）。
    .OUTPUTS @{ ok; pid; log; log_err; command } —— 失败时 ok=$false。
    .NOTES
        调用前必须已执行 Clear-OpsProxyForChild（子进程继承当前环境）。
        -Dev：uvicorn --reload + next dev；生产：uvicorn（无 reload）+ next start。
    #>
    param(
        [string]$Root,
        [ValidateSet('backend', 'frontend')]
        [string]$Which,
        [switch]$Dev,
        [string]$Suffix = ''
    )
    $logsDir = Get-OpsLogsDir -Root $Root
    $ts = Get-OpsTimestamp
    $tag = if ($Suffix) { $ts + '-' + $Suffix } else { $ts }

    if ($Which -eq 'backend') {
        $py = Get-OpsPythonExe -Root $Root
        if (-not (Test-Path -LiteralPath $py)) {
            return @{ ok = $false; pid = 0; log = ''; log_err = ''; command = '' }
        }
        $outLog = Join-Path $logsDir ("backend-" + $tag + ".log")
        $errLog = Join-Path $logsDir ("backend-" + $tag + ".err.log")
        $args = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000')
        if ($Dev) { $args = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000', '--reload') }
        $proc = Start-Process -FilePath $py -ArgumentList $args `
            -WorkingDirectory (Join-Path $Root 'backend') `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
            -WindowStyle Hidden -PassThru
        $command = if ($Dev) { 'uvicorn app.main:app --reload --port 8000' } else { 'uvicorn app.main:app --port 8000' }
        return @{ ok = $true; pid = [int]$proc.Id; proc = $proc; log = $outLog; log_err = $errLog; command = $command }
    }
    else {
        $node = Get-OpsNodeExe
        $nextJs = Get-OpsNextJs -Root $Root
        if (-not $node -or -not (Test-Path -LiteralPath $nextJs)) {
            return @{ ok = $false; pid = 0; log = ''; log_err = ''; command = '' }
        }
        $outLog = Join-Path $logsDir ("frontend-" + $tag + ".log")
        $errLog = Join-Path $logsDir ("frontend-" + $tag + ".err.log")
        $mode = if ($Dev) { 'dev' } else { 'start' }
        # -H 127.0.0.1：next 默认监听所有网卡，这里强制只绑本机回环（与全项目「只绑 127.0.0.1」一致）
        $args = @($nextJs, $mode, '-p', '3000', '-H', '127.0.0.1')
        $proc = Start-Process -FilePath $node -ArgumentList $args `
            -WorkingDirectory (Join-Path $Root 'frontend') `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
            -WindowStyle Hidden -PassThru
        $command = if ($Dev) { 'next dev -p 3000 -H 127.0.0.1' } else { 'next start -p 3000 -H 127.0.0.1' }
        return @{ ok = $true; pid = [int]$proc.Id; proc = $proc; log = $outLog; log_err = $errLog; command = $command }
    }
}

function Get-OpsServiceLogHint {
    param(
        [string]$Root,
        [string]$LogPath
    )
    return (Get-OpsRelPath -Root $Root -Path $LogPath)
}


# ============================ 计划任务（可撤销注册） ============================

function Get-OpsTaskFullName {
    param([string]$TaskName)
    return ('\' + $script:OpsTaskFolderName + '\' + $TaskName)
}

function Get-OpsFullUserName {
    # 「域\用户名」全名（登录触发器的 -User 参数必须要全名；
    # 实测：不带 -User 的 AtLogOn 触发器 = 「任意用户登录」→ 注册被系统拒绝）
    return ($env:USERDOMAIN + '\' + $env:USERNAME)
}

function Get-OpsScheduledTask {
    param([string]$TaskName)
    try {
        return (Get-ScheduledTask -TaskName $TaskName -TaskPath ('\' + $script:OpsTaskFolderName + '\') -ErrorAction Stop)
    } catch {
        return $null
    }
}

function Register-OpsScheduledTask {
    <#
    .SYNOPSIS 注册（或更新）计划任务并【回读验证】。
    .DESCRIPTION
        注册前先 Unregister 同名任务（-Force 亦可，但显式清理更可审计）。
        注册后必须能通过 Get-ScheduledTask 回读到，才算成功 —— 绝不谎报。
        Register/Unregister 一律 -ErrorAction Stop（被系统拒绝时如实失败）。
    .OUTPUTS $null 或任务对象。
    #>
    param(
        [string]$TaskName,
        $Action,
        $Trigger,
        $Settings,
        $Principal
    )
    $folder = '\' + $script:OpsTaskFolderName + '\'
    try {
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath $folder -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        Register-ScheduledTask -TaskName $TaskName -TaskPath $folder `
            -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal `
            -Force -ErrorAction Stop | Out-Null
    } catch {
        Write-OpsBad ('计划任务注册失败：' + (Get-OpsSafeText -Text $_.Exception.Message))
        if ([string]$_.Exception.Message -match '拒绝访问|Access is denied') {
            Write-OpsInfo '可能原因：本会话权限不足（任务计划可能需要管理员），或被系统策略限制。'
            Write-OpsInfo '可尝试：以管理员身份打开 PowerShell 后重新运行本脚本。'
        }
        return $null
    }
    $task = Get-OpsScheduledTask -TaskName $TaskName
    if ($null -eq $task) {
        Write-OpsBad '计划任务注册后回读失败（未找到），视为注册未成功。'
        return $null
    }
    return $task
}

function Unregister-OpsScheduledTask {
    <#
    .SYNOPSIS 卸载计划任务并【回读验证】已消失；尝试顺带删除空的任务文件夹。
    .OUTPUTS $true = 卸载完成（或本就不存在）；$false = 卸载失败。
    #>
    param([string]$TaskName)
    $folder = '\' + $script:OpsTaskFolderName + '\'
    $existing = Get-OpsScheduledTask -TaskName $TaskName
    if ($null -eq $existing) {
        Remove-OpsTaskFolderQuiet
        return $true
    }
    try {
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath $folder -Confirm:$false -ErrorAction Stop
    } catch {
        Write-OpsBad ('计划任务卸载失败：' + (Get-OpsSafeText -Text $_.Exception.Message))
        return $false
    }
    $after = Get-OpsScheduledTask -TaskName $TaskName
    if ($null -ne $after) {
        Write-OpsBad '卸载后回读仍存在，视为卸载未完成。'
        return $false
    }
    Remove-OpsTaskFolderQuiet
    return $true
}

function Remove-OpsTaskFolderQuiet {
    <# 尝试删除（已被清空的）xhs-agent 任务文件夹；失败不影响结果。 #>
    try {
        $svc = New-Object -ComObject Schedule.Service
        $svc.Connect()
        $root = $svc.GetFolder('\')
        # 仅在文件夹为空时删除（DeleteFolder 的第二参 = 0 表示非空则报错）
        $root.DeleteFolder($script:OpsTaskFolderName, 0)
    } catch { }
}


# ============================ 前端构建判定 ============================

function Test-OpsFrontendBuildNeeded {
    <#
    .SYNOPSIS 判断生产模式是否需要重新构建前端（.next 缺失或源码/配置更新）。
    .OUTPUTS @{ needed = $true/$false; reason = '说明' }
    .NOTES
        以 .next\BUILD_ID 的写入时刻为基准，比较 frontend\src、package.json、
        next.config.* 里最新的修改时刻；不依赖任何外部工具，纯文件时间戳。
    #>
    param([string]$Root)
    $fe = Join-Path $Root 'frontend'
    $buildId = Join-Path $fe '.next\BUILD_ID'
    if (-not (Test-Path -LiteralPath $buildId)) {
        return @{ needed = $true; reason = '未找到 .next 构建产物' }
    }
    try {
        $buildTime = (Get-Item -LiteralPath $buildId).LastWriteTimeUtc
    } catch {
        return @{ needed = $true; reason = '构建产物时间戳不可读' }
    }
    $watch = @(
        (Join-Path $fe 'src'),
        (Join-Path $fe 'package.json'),
        (Join-Path $fe 'next.config.ts'),
        (Join-Path $fe 'next.config.js'),
        (Join-Path $fe 'next.config.mjs')
    )
    $newest = $null
    foreach ($item in $watch) {
        if (-not (Test-Path -LiteralPath $item)) { continue }
        try {
            $gi = Get-Item -LiteralPath $item
            if ($gi.PSIsContainer) {
                $f = Get-ChildItem -LiteralPath $item -Recurse -File -ErrorAction SilentlyContinue |
                    Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
                if ($f) {
                    if ($null -eq $newest -or $f.LastWriteTimeUtc -gt $newest) { $newest = $f.LastWriteTimeUtc }
                }
            } else {
                if ($null -eq $newest -or $gi.LastWriteTimeUtc -gt $newest) { $newest = $gi.LastWriteTimeUtc }
            }
        } catch { }
    }
    if ($newest -and $newest -gt $buildTime) {
        return @{ needed = $true; reason = '前端源码或配置比上次构建更新' }
    }
    return @{ needed = $false; reason = '构建产物为最新' }
}

