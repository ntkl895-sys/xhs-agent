#Requires -Version 5.1
<#
.SYNOPSIS
    xhs-agent · 注册「登录后自动启动」（Windows 计划任务，默认不启用、可撤销）

.DESCRIPTION
    注册一个绑定【当前用户登录】的计划任务：登录后延迟 N 秒自动执行
    scripts\ops\start.ps1 -AutoDocker -NoBrowser（不弹浏览器；服务静默拉起）。

    方式取舍（计划任务 vs 启动文件夹）：
      - 计划任务（本脚本采用）：窗口完全隐藏、支持登录后延迟（给 Docker Desktop
        留启动时间）、配 -Watch 可做崩溃自愈、可在「任务计划程序」中随时查看/停用；
        实测普通权限可注册，但登录触发器【必须绑定具体用户】
        （「任意用户登录」触发器需要管理员权限，本脚本已按用户绑定处理）。
      - 启动文件夹：实现更简单，但登录时会闪出控制台窗口、无延迟能力；本工程未采用。

    ⚠️ 默认【不启用】：只有显式运行本脚本才会注册；
    ⚠️ 撤销：运行 uninstall-autostart.ps1（或任务计划程序里删除任务），删除即复原；
    ⚠️ 本脚本只写入任务计划程序，不改动任何其他系统设置。

    典型用法：
        powershell -ExecutionPolicy Bypass -File scripts\ops\install-autostart.ps1
        powershell -ExecutionPolicy Bypass -File scripts\ops\install-autostart.ps1 -Watch  # 附带崩溃自愈

    退出码约定：0 = 注册成功（已回读验证）；1 = 注册失败（含权限不足）
#>
[CmdletBinding()]
param(
    [string]$Root = '',
    [int]$DelaySeconds = 45,
    [switch]$Watch
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ops-common.ps1')
Initialize-OpsConsole

try {
    $Root = Resolve-OpsRoot -ScriptRoot $PSScriptRoot -Override $Root
    $taskName = 'xhs-agent-autostart'
    $startScript = Join-Path $Root 'scripts\ops\start.ps1'
    $pwshExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $fullUser = Get-OpsFullUserName

    Write-Host '=================================================='
    Write-Host ' xhs-agent · 注册「登录后自动启动」（计划任务）'
    Write-Host '=================================================='

    if (-not (Test-Path -LiteralPath $startScript)) {
        Write-OpsBad '未找到 scripts\ops\start.ps1。'
        exit 1
    }
    if (-not (Test-Path -LiteralPath $pwshExe)) {
        Write-OpsBad '未找到系统 PowerShell（Windows PowerShell 5.1）。'
        exit 1
    }

    $actionArgs = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -AutoDocker -NoBrowser' -f $startScript
    if ($Watch) { $actionArgs += ' -Watch' }

    $action = New-ScheduledTaskAction -Execute $pwshExe -Argument $actionArgs -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $fullUser
    $trigger.Delay = ('PT' + [Math]::Max(0, $DelaySeconds) + 'S')
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId $fullUser -LogonType Interactive -RunLevel Limited

    Write-OpsStep '注册计划任务'
    $task = Register-OpsScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal
    if ($null -eq $task) {
        Write-OpsBad '注册未成功（见上方原因），未写入任何内容。'
        exit 1
    }

    Write-OpsStep '注册结果（已回读验证）'
    Write-OpsOk ('任务：' + $task.TaskName + '（位于任务文件夹 xhs-agent，状态 ' + $task.State + '）')
    Write-OpsInfo ('触发器：当前用户（' + $fullUser + '）登录后延迟 ' + [Math]::Max(0, $DelaySeconds) + ' 秒')
    Write-OpsInfo ('动作：scripts\ops\start.ps1 -AutoDocker -NoBrowser' + $(if ($Watch) { ' -Watch' } else { '' }))
    Write-OpsInfo '说明：窗口完全隐藏；到点后自动拉起数据库容器与前后端（已就绪则跳过）。'
    Write-Host ''
    Write-OpsInfo '撤销方式（删除即复原）：'
    Write-OpsInfo '    powershell -ExecutionPolicy Bypass -File scripts\ops\uninstall-autostart.ps1'
    Write-Host ''
    Write-OpsOk '自动启动已注册（默认不启用的承诺仍然有效：本次为显式注册）。'
    exit 0
} catch {
    Write-OpsBad ('注册过程出现意外错误：' + (Get-OpsSafeText -Text $_.Exception.Message))
    exit 1
}
