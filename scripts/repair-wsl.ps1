<#
.SYNOPSIS
    xhs-agent · WSL 彻底卸载重装修复脚本（Windows 11 24H2/25H2）

.DESCRIPTION
    用于修复 wsl 报错：
        wsl: WSL 安装似乎已损坏 (错误代码：Wsl/CallMsi/Install/REGDB_E_CLASSNOTREG)
        没有注册类

    成因（微软官方仓库 microsoft/WSL issue #10882 已确认）：
    Windows 11 24H2/25H2 起，WSL 不再完整捆绑于系统。旧的 MSI/COM 注册层一旦损坏，
    `wsl --update` / `wsl --install` 均无法自愈，必须【彻底卸载后重装】。
    现象特征：WSL 应用包 Status 显示为 Ok（包本身完好），但任何 wsl 命令都报错。

    本脚本按【阶段 phase】推进，状态记录在：
        %ProgramData%\xhs-agent\wsl-repair.state
    因此可以安全地重复运行；两次重启后再次运行即可自动续跑。
    全程【不会自动重启电脑】，只在需要重启的环节停下并明确提示。

    请以【管理员身份】打开 PowerShell 后运行：
        powershell -ExecutionPolicy Bypass -File scripts\repair-wsl.ps1

    从零重来（清除阶段记录）：
        powershell -ExecutionPolicy Bypass -File scripts\repair-wsl.ps1 -StartOver

    退出码约定：
        0    = WSL 已修复并验证通过
        1    = 当前不是管理员权限
        3010 = 需要【重启电脑】后再次运行本脚本（正常中间态，不是错误）
        4    = 自动流程未能完成，需按屏幕提示人工处理

.NOTES
    本脚本只操作系统 WSL 相关的【应用包 / 可选功能 / 服务】，不触碰项目文件，
    也不改动 Docker 相关配置。所有系统级改动均通过官方工具（DISM / winget / Store）完成。
#>

param(
    [switch]$StartOver
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

# ============================ 输出辅助 ============================
function Info([string]$t) { Write-Host $t }
function Step([string]$t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Ok  ([string]$t) { Write-Host "  [OK] $t" -ForegroundColor Green }
function Warn([string]$t) { Write-Host "  [!]  $t" -ForegroundColor Yellow }
function Bad ([string]$t) { Write-Host "  [X] $t" -ForegroundColor Red }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ============================ 阶段状态 ============================
$StateDir  = Join-Path $env:ProgramData "xhs-agent"
$StateFile = Join-Path $StateDir "wsl-repair.state"

function Get-Phase {
    if (Test-Path $StateFile) {
        $v = (Get-Content $StateFile -Raw -ErrorAction SilentlyContinue)
        if ($v) { $v = $v.Trim() }
        if ($v -match '^\d+$') { return [int]$v }
    }
    return 0
}
function Set-Phase([int]$p) {
    if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
    Set-Content -Path $StateFile -Value $p -Encoding ASCII
}
function Clear-Phase {
    if (Test-Path $StateFile) { Remove-Item $StateFile -Force -ErrorAction SilentlyContinue }
}

# ============================ 有界执行 wsl ============================
# wsl 在损坏状态下会弹「按任意键修复…60 秒后超时」的交互提示，
# 直接调用可能阻塞最长 60 秒。这里用 .NET Process 执行、立即关闭 stdin，
# 并加硬超时，确保探测不会把脚本卡住。
function Invoke-WslBounded([string[]]$argv, [int]$timeoutMs = 18000) {
    $p = New-Object System.Diagnostics.Process
    $p.StartInfo.FileName               = "wsl.exe"
    $p.StartInfo.Arguments              = ($argv -join ' ')
    $p.StartInfo.UseShellExecute        = $false
    $p.StartInfo.CreateNoWindow         = $true
    $p.StartInfo.RedirectStandardInput  = $true
    $p.StartInfo.RedirectStandardOutput = $true
    $p.StartInfo.RedirectStandardError  = $true
    try { $null = $p.Start() } catch { return @{ ok = $false; text = ""; exit = -1 } }
    try { $p.StandardInput.Close() } catch { }   # 立刻闭合 stdin，规避交互提示阻塞
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($timeoutMs)) {
        try { $p.Kill() } catch { }
        try { $null = $p.WaitForExit(3000) } catch { }
        return @{ ok = $false; text = ""; exit = -1 }
    }
    $txt = ""
    try { $txt = $outTask.Result } catch { }
    try { $txt += $errTask.Result } catch { }
    return @{ ok = $true; text = $txt; exit = $p.ExitCode }
}

function Get-WslState {
    # 返回 "ok" / "broken" / "missing"
    $wsl = Get-Command wsl -ErrorAction SilentlyContinue
    if (-not $wsl) { return "missing" }
    $r = Invoke-WslBounded @("--version") 18000
    if ($r.ok -and $r.exit -eq 0 -and $r.text -match "WSL") { return "ok" }
    $r2 = Invoke-WslBounded @("--status") 18000
    if ($r2.ok -and $r2.exit -eq 0) { return "ok" }
    return "broken"
}

function Test-FeatureEnabled([string]$name) {
    try {
        $f = Get-WindowsOptionalFeature -Online -FeatureName $name -ErrorAction Stop
        return ($f.State -eq "Enabled")
    } catch { return $false }
}

# ============================ 主流程 ============================
Info "=============================================================="
Info " xhs-agent · WSL 彻底卸载重装修复（Windows 11 24H2/25H2）"
Info "=============================================================="

if ($StartOver) {
    Clear-Phase
    Warn "已清除阶段记录，将从「阶段 1」重新开始。"
}

# ---- 环境检查 --------------------------------------------------------------
Step "环境检查"
if (-not (Test-Admin)) {
    Bad "当前不是管理员权限。"
    Info ""
    Info "请以【管理员身份】打开 PowerShell，再执行："
    Info "    powershell -ExecutionPolicy Bypass -File scripts\repair-wsl.ps1"
    exit 1
}
Ok "管理员权限检查通过。"

$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue
if ($cv) {
    Info ("  Windows：" + $cv.ProductName + " " + $cv.DisplayVersion + " (Build " + $cv.CurrentBuild + "." + $cv.UBR + ")")
}

$st = Get-WslState
Info ("  当前 WSL 状态：" + $st)

if ((-not $StartOver) -and ($st -eq "ok")) {
    Ok "WSL 目前可用，无需修复。"
    Info ""
    Info "  如需确认，可执行：wsl --status"
    Info "  修复完成后，可继续安装 Docker Desktop："
    Info "      powershell -ExecutionPolicy Bypass -File scripts\install-docker.ps1"
    Clear-Phase
    exit 0
}

$phase = Get-Phase
Info ("  修复阶段：" + $phase + " / 3")

# ---- 阶段 1：卸载应用包 + 关闭系统功能 --------------------------------------
if ($phase -eq 0) {
    Step "阶段 1/3：卸载 WSL 应用包，并关闭相关系统功能"

    Info "  (1) 停止 WSL 相关服务与进程 …"
    $null = Invoke-WslBounded @("--shutdown") 10000
    foreach ($svc in @('WSLService','LxssManager')) {
        try {
            Stop-Service -Name $svc -Force -ErrorAction Stop
            Info ("      已停止服务：" + $svc)
        } catch {
            Info ("      服务不存在或已停止：" + $svc)
        }
    }
    try { Get-Process wslservice, wsl -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue } catch { }

    Info "  (2) 卸载 Store / MSIX 形态的 WSL 应用包 …"
    $pkgs = @(Get-AppxPackage -AllUsers -Name MicrosoftCorporationII.WindowsSubsystemForLinux -ErrorAction SilentlyContinue)
    if ($pkgs.Count -gt 0) {
        foreach ($p in $pkgs) {
            try {
                Remove-AppxPackage -Package $p.PackageFullName -AllUsers -ErrorAction Stop
                Ok ("      已卸载：" + $p.PackageFullName)
            } catch {
                Warn ("      卸载失败：" + $p.PackageFullName + " —— " + $_.Exception.Message)
                Warn ("      若报 0x80073cfb（已为其他用户安装），请重启电脑后再运行本脚本重试。")
            }
        }
    } else {
        Info "      未发现 Store 形态的 WSL 应用包。"
    }

    Info "  (3) 处理 MSI 形态的 WSL（若存在 C:\Program Files\WSL）…"
    $msiDir = Join-Path $env:ProgramFiles "WSL"
    if (Test-Path $msiDir) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($winget) {
            try { & winget uninstall -e --id Microsoft.WSL --accept-source-agreements *> $null } catch { }
        }
        Warn "      检测到 MSI 形态安装目录。若未被自动卸载，请到「设置 → 应用 → 已安装的应用」中卸载「Windows Subsystem for Linux」。"
    } else {
        Info "      未发现 MSI 形态安装目录。"
    }

    Info "  (4) 关闭两个必需的系统功能 …"
    foreach ($f in @('Microsoft-Windows-Subsystem-Linux','VirtualMachinePlatform')) {
        try {
            & dism.exe /online /disable-feature /featurename:$f /norestart *> $null
            Info ("      已请求关闭：" + $f + "（dism 返回码 " + $LASTEXITCODE + "）")
        } catch {
            Warn ("      关闭失败：" + $f)
        }
    }

    Set-Phase 1
    Info ""
    Info "=============================================================="
    Warn "【需要重启电脑】才能继续。"
    Info "  重启后，请再次以管理员身份运行本脚本（会自动进入阶段 2）："
    Info "      powershell -ExecutionPolicy Bypass -File scripts\repair-wsl.ps1"
    Info "=============================================================="
    exit 3010
}

# ---- 阶段 2：重新启用系统功能 ----------------------------------------------
if ($phase -eq 1) {
    Step "阶段 2/3：重新启用系统功能"

    $needReboot = $false
    foreach ($f in @('Microsoft-Windows-Subsystem-Linux','VirtualMachinePlatform')) {
        if (Test-FeatureEnabled $f) {
            Ok ("      已启用（跳过）：" + $f)
            continue
        }
        try {
            & dism.exe /online /enable-feature /featurename:$f /all /norestart *> $null
            Info ("      已请求启用：" + $f + "（dism 返回码 " + $LASTEXITCODE + "）")
            $needReboot = $true
        } catch {
            Warn ("      启用失败：" + $f)
        }
    }

    if ($needReboot) {
        Set-Phase 2
        Info ""
        Info "=============================================================="
        Warn "【需要重启电脑】才能继续。"
        Info "  重启后，请再次以管理员身份运行本脚本（会自动进入阶段 3）："
        Info "      powershell -ExecutionPolicy Bypass -File scripts\repair-wsl.ps1"
        Info "=============================================================="
        exit 3010
    }

    Info "  两个功能均已处于启用状态，无需重启，继续阶段 3。"
    $phase = 2
}

# ---- 阶段 3：重新安装 WSL --------------------------------------------------
if ($phase -eq 2) {
    Step "阶段 3/3：重新安装 WSL"

    # (0) 预检：新应用的默认安装位置必须在【系统盘】。
    #     若被设成非系统盘，Store 安装会报 0x80073D21（Appx 强制系统盘校验）。
    try {
        $vols = Get-AppxVolume -ErrorAction Stop
        foreach ($v in $vols) {
            Info ("  Appx 卷：" + $v.PackageStorePath + "   系统卷=" + $v.IsSystemVolume)
        }
    } catch { }
    Info "  提示：若 Store 报 0x80073D21，请到「设置 → 系统 → 存储 → 高级存储设置 →"
    Info "        保存新内容的位置」，把「新的应用将保存到」改为 C:（系统盘）后重试。"

    $installed = $false
    $winget = Get-Command winget -ErrorAction SilentlyContinue

    # (0.5) 若 WSL 应用包已存在（例如上一次已通过 Store 界面手动装好），直接跳过重复安装。
    #       重复安装只会再次命中原有的 0x80073D21 / 0x80072efd，既无意义也拖慢流程。
    $existing = @(Get-AppxPackage -AllUsers -Name MicrosoftCorporationII.WindowsSubsystemForLinux -ErrorAction SilentlyContinue)
    if ($existing.Count -gt 0) {
        Ok "  已检测到 WSL 应用包，跳过重复安装（直接进入「重启 → 验证」）："
        foreach ($e in $existing) {
            Info ("      " + $e.Name + "  " + $e.Version + "  状态=" + $e.Status)
            Info ("      安装位置：" + $e.InstallLocation)
        }
        $installed = $true
    }

    # (a) 首选：Microsoft Store 源 —— 走微软 CDN，可绕开本机对 GitHub 的网络限制
    if ($winget) {
        Info "  (a) 通过【Microsoft Store 源】安装 WSL（推荐，走微软 CDN）…"
        try {
            & winget install --id 9P9TQF7MRM4R --source msstore --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) { Ok "      安装命令执行完成。"; $installed = $true }
            else { Warn ("      msstore 源返回码：" + $LASTEXITCODE) }
        } catch {
            Warn ("      msstore 源异常：" + $_.Exception.Message)
        }
    }

    # (b) 次选：winget 官方源 —— 其安装包下载地址指向 GitHub，网络受限时可能失败
    if ((-not $installed) -and $winget) {
        Info "  (b) 通过【winget 官方源】安装 WSL（下载源为 GitHub）…"
        try {
            & winget install -e --id Microsoft.WSL --source winget --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) { Ok "      安装命令执行完成。"; $installed = $true }
            else { Warn ("      winget 返回码：" + $LASTEXITCODE) }
        } catch {
            Warn ("      winget 异常：" + $_.Exception.Message)
        }
    }

    # (c) 备选：经本机代理直接下载官方 MSI 并离线安装
    #     可绕开 Store(0x80073D21) 与 winget(0x80072efd 连不上 GitHub) 两类限制。
    if (-not $installed) {
        $proxy = $env:HTTPS_PROXY
        if (-not $proxy) { $proxy = $env:HTTP_PROXY }
        if ($proxy) {
            Info ("  (c) 检测到代理 " + $proxy + "，尝试经代理下载官方 WSL MSI 并离线安装 …")
            $curlArgs = @("-sL", "--fail")
            try {
                # 先查 GitHub API，取最新 release 里的 x64 MSI 下载地址（不写死版本号）
                $apiJson = (& curl.exe @curlArgs --proxy $proxy "https://api.github.com/repos/microsoft/WSL/releases/latest" | Out-String)
                $rel = $apiJson | ConvertFrom-Json
                $asset = $rel.assets | Where-Object { $_.name -match '^wsl\..*x64\.msi$' } | Select-Object -First 1
                if (-not $asset) { throw "未在 release 中找到 x64 MSI 资源" }
                $url = $asset.browser_download_url
                $msi = Join-Path $env:TEMP $asset.name
                Info ("      目标：" + $url)

                # 大文件下载用 —output（-o），经代理
                & curl.exe -L --fail --proxy $proxy -o $msi $url
                if ($LASTEXITCODE -eq 0 -and (Test-Path $msi) -and ((Get-Item $msi).Length -gt 10MB)) {
                    Ok ("      下载完成：" + $msi + "（" + [math]::Round((Get-Item $msi).Length / 1MB, 1) + " MB）")
                    Info "      开始安装（可能耗时数分钟）…"
                    & msiexec.exe /i $msi /norestart /passive
                    if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq 3010) { Ok "      MSI 安装完成。"; $installed = $true }
                    else { Warn ("      MSI 安装返回码：" + $LASTEXITCODE) }
                } else {
                    Warn ("      经代理下载失败（curl 返回码 " + $LASTEXITCODE + "）。")
                }
            } catch {
                Warn ("      代理下载方案异常：" + $_.Exception.Message)
            }
        } else {
            Info "  (c) 未检测到 HTTP_PROXY / HTTPS_PROXY，跳过「代理下载 + 离线安装」方案。"
        }
    }

    # (d) 兜底：打开 Microsoft Store 页面，人工点击安装
    if (-not $installed) {
        Warn "  自动安装未成功，正在打开 Microsoft Store 的 WSL 页面 …"
        try { Start-Process "ms-windows-store://pdp/?ProductId=9P9TQF7MRM4R" } catch { }
        Info ""
        Info "  请在弹出的商店页面点击【获取 / 安装】，等待装完。"
        Info "  若商店打不开或报 0x80073D21，先运行 wsreset.exe 重置商店缓存，"
        Info "  并确认「设置 → 系统 → 存储 → 高级存储设置 → 保存新内容的位置」为 C:。"
        Read-Host "  安装完成后按【回车】继续"
    }

    Set-Phase 3
    Info ""
    Info "=============================================================="
    Warn "【需要重启电脑】以完成 WSL 组件注册。"
    Info "  重启后，请再次以管理员身份运行本脚本（会自动进入验证）："
    Info "      powershell -ExecutionPolicy Bypass -File scripts\repair-wsl.ps1"
    Info "=============================================================="
    exit 3010
}

# ---- 阶段 4：验证 ----------------------------------------------------------
if ($phase -eq 3) {
    Step "验证 WSL"

    $st2 = Get-WslState
    if ($st2 -eq "ok") {
        Ok "WSL 已可用。"
        $null = Invoke-WslBounded @("--set-default-version","2") 15000
        $ver = Invoke-WslBounded @("--version") 18000
        if ($ver.ok -and $ver.text.Trim()) { Info ("  " + $ver.text.Trim()) }
        Clear-Phase
        Info ""
        Info "=============================================================="
        Ok "WSL 修复完成。下一步：安装并启动 Docker Desktop"
        Info "      powershell -ExecutionPolicy Bypass -File scripts\install-docker.ps1"
        Info "=============================================================="
        exit 0
    }

    Bad ("WSL 仍不可用（状态：" + $st2 + "）。")
    Set-Phase 2   # 回退到「安装」阶段：处理完下面任一项后，再次运行本脚本即可直接重试安装
    Info ""
    Info "  已将阶段回退为「安装」。处理下面任一项后，【再次运行本脚本】即可重试安装："
    Info "  1) 若日志出现 0x80073D21 —— 把「新的应用将保存到」改为 C:（系统盘）后重试。"
    Info "  2) 若日志出现 0x80072efd / 连不上 GitHub —— 先设好代理再重跑，脚本会自动走代理下载 MSI："
    Info "         $env:HTTPS_PROXY = 'http://<你的代理>:<端口>'"
    Info "  3) 运行系统文件修复（耗时 10~20 分钟，完成后重启）："
    Info "         sfc /scannow"
    Info "         DISM /Online /Cleanup-Image /RestoreHealth"
    Info "  4) 检查杀毒软件 / 系统优化工具是否拦截了 COM 组件注册，临时退出后重试。"
    Info "  5) 确认本机可联网；公司网络 / 代理可能拦截 Microsoft Store 与下载。"
    Info ""
    Info "  如需从阶段 1（卸载 + 关闭系统功能）彻底重来："
    Info "         powershell -ExecutionPolicy Bypass -File scripts\repair-wsl.ps1 -StartOver"
    exit 4
}
