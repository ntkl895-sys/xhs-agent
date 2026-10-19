<#
.SYNOPSIS
    xhs-agent · Docker Desktop 检测与安装脚本（Windows）

.DESCRIPTION
    分步、幂等、可重复运行。只做 Docker 的【检测 / 安装 / 启动 / 验证】，
    不修改任何其他系统设置。所有需要人工介入的环节都会明确提示，绝不谎报成功。

    请以【管理员身份】打开 PowerShell 后运行：
        powershell -ExecutionPolicy Bypass -File scripts\install-docker.ps1

    退出码约定：
        0 = Docker 已就绪（docker info 正常）
        1 = 当前不是管理员权限
        2 = 需要重启电脑后再次运行本脚本
        3 = winget 不可用或安装失败，需按提示手动安装
        4 = 已尝试启动但等待超时，引擎仍未就绪

.NOTES
    Windows 上 Docker Desktop 默认使用 WSL2 后端；安装属系统级操作，
    需管理员权限，可能需先启用 WSL2 并重启，首次启动还要人工接受协议/登录。
#>

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

# ============================ 输出辅助 ============================
function Info([string]$t) { Write-Host $t }
function Step([string]$t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Ok  ([string]$t) { Write-Host "  [OK] $t" -ForegroundColor Green }
function Warn([string]$t) { Write-Host "  [!]  $t" -ForegroundColor Yellow }
function Bad ([string]$t) { Write-Host "  [X]  $t" -ForegroundColor Red }

# ============================ 工具函数 ============================
function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# 刷新当前会话的 PATH，使安装后新出现的 docker.exe 可被找到
function Update-PathFromRegistry {
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = ($machine, $user -ne $null) -join ";"
}

# 解析 docker.exe 的完整路径（可能在标准安装目录、但尚未进入本会话 PATH）
function Get-DockerExe {
    $c = Get-Command docker -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $candidates = @(
        (Join-Path $Env:ProgramFiles "Docker\Docker\resources\bin\docker.exe"),
        (Join-Path ${Env:ProgramFiles(x86)} "Docker\Docker\resources\bin\docker.exe"),
        (Join-Path $Env:LOCALAPPDATA "Docker\Docker\resources\bin\docker.exe")
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

# docker info 是否正常（引擎已运行）
function Test-DockerReady {
    $exe = Get-DockerExe
    if (-not $exe) { return $false }
    try {
        & $exe info *> $null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

# 找并启动 Docker Desktop.exe
function Start-DockerDesktop {
    $candidates = @(
        (Join-Path $Env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        (Join-Path ${Env:ProgramFiles(x86)} "Docker\Docker\Docker Desktop.exe"),
        (Join-Path $Env:LOCALAPPDATA "Docker\Docker Desktop.exe")
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) {
            Info "  正在启动：$p"
            Start-Process -FilePath $p | Out-Null
            return $true
        }
    }
    Warn "未找到 Docker Desktop.exe，可能尚未安装完成。"
    return $false
}

# 轮询等待引擎就绪
function Wait-DockerReady([int]$timeoutSeconds = 180) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    $dotCount = 0
    while ((Get-Date) -lt $deadline) {
        if (Test-DockerReady) { Write-Host ""; return $true }
        Start-Sleep -Seconds 5
        $dotCount++
        if ($dotCount % 6 -eq 0) { Write-Host "  ...仍在等待引擎就绪（已等待 $($dotCount * 5) 秒）" }
    }
    return $false
}

# WSL 是否可用（新版本 --version 成功，或旧版本 --status 成功）
function Test-WslOk {
    $wsl = Get-Command wsl -ErrorAction SilentlyContinue
    if (-not $wsl) { return $false }
    try {
        $out = (& wsl --version 2>&1 | Out-String)
        if ($LASTEXITCODE -eq 0 -and $out -match "WSL") { return $true }
    } catch { }
    try {
        $out2 = (& wsl --status 2>&1 | Out-String)
        if ($LASTEXITCODE -eq 0) { return $true }
    } catch { }
    return $false
}

# 打印手动安装指引
function Show-ManualGuide {
    Info ""
    Info "──────────────────────────────────────────────────────────────"
    Info " 未能通过 winget 自动完成安装，请按以下【手动方式】安装："
    Info "──────────────────────────────────────────────────────────────"
    Info " 1) 下载 Docker Desktop Installer.exe（官方页面）："
    Info "    https://docs.docker.com/desktop/setup/install/windows-install/"
    Info " 2) 安装时【务必勾选】Use WSL 2 instead of Hyper-V（使用 WSL 2 而非 Hyper-V）。"
    Info " 3) 安装完成后从开始菜单启动 Docker Desktop，首次启动需人工接受协议 / 登录。"
    Info " 4) 等待引擎就绪后，用以下命令验证："
    Info "      docker --version"
    Info "      docker compose version"
    Info "      docker info"
    Info "──────────────────────────────────────────────────────────────"
}

# ============================ 主流程 ============================
Info "=================================================="
Info " xhs-agent · Docker Desktop 检测与安装（Windows）"
Info "=================================================="

# ---- 步骤 1：检测是否已就绪 -------------------------------------------------
Step "步骤 1/6：检测 Docker 是否已就绪"
if (Test-DockerReady) {
    $exe = Get-DockerExe
    Ok "Docker 已就绪，无需安装。"
    Info ("  docker 版本：" + ((& $exe --version 2>&1 | Out-String).Trim()))
    $composeOut = (& $exe compose version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -eq 0) { Info ("  docker compose 版本：" + $composeOut) }
    else { Warn "docker compose 子命令不可用，请确认 Docker Desktop 安装完整。" }
    Info ""
    Info "Docker 已就绪"
    exit 0
}

$dockerCliExists = [bool](Get-DockerExe)
if ($dockerCliExists) {
    Warn "检测到 docker 命令，但引擎未运行（docker info 失败）。"
} else {
    Warn "未检测到 docker 命令（尚未安装 Docker Desktop）。"
}

# ---- 步骤 2：管理员权限检查 -------------------------------------------------
Step "步骤 2/6：检查管理员权限"
if (-not (Test-Admin)) {
    Bad "当前不是管理员权限，无法安装系统级软件。"
    Info ""
    Info "请以【管理员身份】打开 PowerShell，再执行："
    Info "    powershell -ExecutionPolicy Bypass -File scripts\install-docker.ps1"
    Info ""
    Info "提示：本脚本不做静默提权；提权后系统会新开一个窗口，请在新窗口里重新运行。"
    exit 1
}
Ok "当前已是管理员权限。"

# ---- 分支：已装 CLI 则跳过 WSL2 / winget，直接启动 ---------------------------
if ($dockerCliExists) {
    Info ""
    Info "已检测到 docker CLI，跳过 WSL2 检查与 winget 安装环节。"
}
else {
    # ---- 步骤 3：检查 WSL2 -------------------------------------------------
    Step "步骤 3/6：检查 WSL2"
    if (Test-WslOk) {
        Ok "WSL2 可用。"
    }
    else {
        Warn "WSL2 不可用或为过旧的内置版本，尝试启用 WSL（不安装 Linux 发行版）……"
        $wsl = Get-Command wsl -ErrorAction SilentlyContinue
        $wslOut = ""
        if ($wsl) {
            $wslOut = (& wsl --install --no-distribution 2>&1 | Out-String)
            if ($wslOut.Trim()) { Info ("  wsl 输出：" + $wslOut.Trim()) }
            Info "  wsl --install 返回码：$LASTEXITCODE"
        } else {
            Warn "未找到 wsl.exe，无法自动启用 WSL。"
        }

        # 再次检测：若仍不可用，区分两种【性质不同】的故障（处理方式完全不同）
        if (-not (Test-WslOk)) {
            $wslBroken = ($wslOut -match "损坏|REGDB_E_CLASSNOTREG|CallMsi")
            if ($wslBroken) {
                Bad "检测到 WSL 组件【安装损坏】（REGDB_E_CLASSNOTREG / Wsl/CallMsi/Install）。"
                Info "  这不是「功能未启用」——重启无法修复，也不是 winget 重装能一句话解决的："
                Info "  必须先【卸载旧组件并关闭系统功能】，重启后再重装，再重启。"
                Info ""
                Info "  推荐：运行本工程专用的修复脚本（分阶段、可重复运行、含两次重启引导）："
                Info "      powershell -ExecutionPolicy Bypass -File scripts\repair-wsl.ps1"
                Info ""
                Info "  手动替代方案（任选其一）："
                Info "   a) Microsoft Store 源安装（走微软 CDN，可绕开 GitHub 网络限制）："
                Info "        winget install --id 9P9TQF7MRM4R --source msstore --accept-package-agreements --accept-source-agreements"
                Info "   b) winget 官方源（下载源指向 GitHub，网络受限时可能失败）："
                Info "        winget install -e --id Microsoft.WSL --accept-package-agreements --accept-source-agreements"
                Info "   c) 手动下载官方 MSI：https://github.com/microsoft/WSL/releases/latest"
                Info ""
                Info "  修复完成并【重启电脑】后，再以管理员身份重新运行本脚本。"
                exit 2
            }
            Warn "WSL2 仍未就绪，该步骤通常需要【重启电脑】才能生效。"
            Info ""
            Info "请【重启电脑】后，再次以管理员身份运行本脚本："
            Info "    powershell -ExecutionPolicy Bypass -File scripts\install-docker.ps1"
            Info ""
            Info "若重启后仍报「WSL 安装似乎已损坏（REGDB_E_CLASSNOTREG）」，请改用重装方式："
            Info "    winget install -e --id Microsoft.WSL --accept-package-agreements --accept-source-agreements"
            exit 2
        }
        Ok "WSL2 已就绪。"
    }

    # ---- 步骤 4：winget 安装 Docker Desktop --------------------------------
    Step "步骤 4/6：使用 winget 安装 Docker Desktop"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    $installed = $false
    if ($winget) {
        Info "  已检测到 winget。开始安装（可能需要数分钟，期间弹出 UAC 或安装界面属正常）……"
        try {
            & winget install -e --id Docker.DockerDesktop --source winget --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) {
                Ok "winget 安装命令执行完成。"
                $installed = $true
            } else {
                Warn "winget 返回码：$LASTEXITCODE，视为未成功。"
            }
        } catch {
            Warn ("winget 执行异常：" + $_.Exception.Message)
        }
    } else {
        Warn "未检测到 winget。"
    }

    if (-not $installed) {
        Show-ManualGuide
        exit 3
    }
    # 安装后刷新 PATH，便于找到新装的 docker.exe
    Update-PathFromRegistry
}

# ---- 步骤 5：启动并等待引擎就绪 ---------------------------------------------
Step "步骤 5/6：启动 Docker Desktop 并等待引擎就绪"
$started = Start-DockerDesktop
Info "  提示：首次启动可能需要在 Docker Desktop 界面中【接受服务协议】或【登录账号】，"
Info "        请在弹出的窗口中完成后，耐心等待引擎就绪（最多约 3 分钟）。"
$ready = Wait-DockerReady -timeoutSeconds 180
if (-not $ready) {
    Bad "等待超时，Docker 引擎仍未就绪。"
    Info ""
    Info "请依次排查："
    Info "  1) 确认 Docker Desktop 已从开始菜单启动，且窗口已打开；"
    Info "  2) Settings → Resources → WSL integration 已启用；"
    Info "  3) 首次启动是否还有【接受协议 / 登录】弹窗未完成；"
    Info "  4) WSL2 内核过旧时执行：wsl --update；"
    Info "  5) 公司网络 / 代理可能影响启动与镜像拉取，必要时配置代理。"
    Info ""
    Info "处理完成后，可重新运行本脚本或直接执行：docker info"
    exit 4
}
Ok "Docker 引擎已就绪。"

# ---- 步骤 6：验证与打印 -----------------------------------------------------
Step "步骤 6/6：验证"
Update-PathFromRegistry
$exe = Get-DockerExe
Info ("  docker 版本：" + ((& $exe --version 2>&1 | Out-String).Trim()))
$composeOut = (& $exe compose version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -eq 0) { Ok ("docker compose 版本：" + $composeOut) }
else { Warn "docker compose 子命令不可用，请确认 Docker Desktop 安装完整。" }

Info ""
Info "  可选：运行 docker run hello-world 验证能否拉取镜像（需联网）……"
try {
    & $exe run --rm hello-world *> $null
    if ($LASTEXITCODE -eq 0) { Ok "hello-world 运行成功。" }
    else { Warn "hello-world 未成功（可能是网络 / 代理问题，不影响 Docker 基本可用）。" }
} catch {
    Warn "hello-world 未成功（可能是网络 / 代理问题，不影响 Docker 基本可用）。"
}

Info ""
Info "=================================================="
if (Test-DockerReady) {
    Info " Docker 已就绪，可以继续执行：docker compose up -d"
    exit 0
} else {
    Bad "Docker 验证未通过，请按上面的排查建议处理后重试。"
    exit 4
}
