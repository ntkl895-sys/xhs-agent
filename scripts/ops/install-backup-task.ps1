#Requires -Version 5.1
<#
.SYNOPSIS
    xhs-agent · 注册「每日定时备份」（Windows 计划任务，默认不启用、可撤销）

.DESCRIPTION
    注册一个每日定时计划任务：到点后隐藏窗口执行 scripts\ops\backup.ps1
    （pg_dump -Fc → backups\，默认保留最近 7 份）。
    说明：
      - 只会创建【当前用户】的任务（Interactive，仅登录时运行——Docker Desktop
        属于用户会话，注销后容器通常也停止，因此登录态运行是正确语义）；
      - StartWhenAvailable = 「错过计划则尽快补跑一次」（coalesce 语义；休眠 / 关机
        错过时点后，唤醒 / 登录后补跑），失败不弹窗打扰，结果写 logs\backup-history.log；
      - 默认【不启用】：只有显式运行本脚本才会注册；
      - 撤销：uninstall-backup-task.ps1（删除即复原）。

    典型用法：
        powershell -ExecutionPolicy Bypass -File scripts\ops\install-backup-task.ps1
        powershell -ExecutionPolicy Bypass -File scripts\ops\install-backup-task.ps1 -DailyAt 03:30

    退出码约定：0 = 注册成功（已回读验证）；1 = 注册失败（含权限不足）
#>
[CmdletBinding()]
param(
    [string]$Root = '',
    [string]$DailyAt = '03:30'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ops-common.ps1')
Initialize-OpsConsole

try {
    $Root = Resolve-OpsRoot -ScriptRoot $PSScriptRoot -Override $Root
    $taskName = 'xhs-agent-backup'
    $backupScript = Join-Path $Root 'scripts\ops\backup.ps1'
    $pwshExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $fullUser = Get-OpsFullUserName

    Write-Host '=================================================='
    Write-Host ' xhs-agent · 注册「每日定时备份」（计划任务）'
    Write-Host '=================================================='

    if ($DailyAt -notmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') {
        Write-OpsBad ('时间格式非法（应为 HH:mm，24 小时制）：' + $DailyAt)
        exit 1
    }
    if (-not (Test-Path -LiteralPath $backupScript)) {
        Write-OpsBad '未找到 scripts\ops\backup.ps1。'
        exit 1
    }

    $actionArgs = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $backupScript
    $action = New-ScheduledTaskAction -Execute $pwshExe -Argument $actionArgs -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Daily -At $DailyAt
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    $principal = New-ScheduledTaskPrincipal -UserId $fullUser -LogonType Interactive -RunLevel Limited

    Write-OpsStep '注册计划任务'
    $task = Register-OpsScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal
    if ($null -eq $task) {
        Write-OpsBad '注册未成功（见上方原因），未写入任何内容。'
        exit 1
    }

    Write-OpsStep '注册结果（已回读验证）'
    Write-OpsOk ('任务：' + $task.TaskName + '（位于任务文件夹 xhs-agent，状态 ' + $task.State + '）')
    Write-OpsInfo ('触发器：每天 ' + $DailyAt + '（当前用户 ' + $fullUser + ' 登录时运行）')
    Write-OpsInfo '动作：scripts\ops\backup.ps1（窗口隐藏；失败不弹窗）'
    Write-OpsInfo '结果查看：logs\backup-history.log；备份文件在 backups\（默认保留最近 7 份）'
    Write-OpsInfo '错过时点后的补跑：StartWhenAvailable 已启用（休眠 / 关机错过 → 唤醒 / 登录后补跑一次）'
    Write-Host ''
    Write-OpsInfo '撤销方式（删除即复原）：'
    Write-OpsInfo '    powershell -ExecutionPolicy Bypass -File scripts\ops\uninstall-backup-task.ps1'
    Write-Host ''
    Write-OpsOk '定时备份已注册（默认不启用的承诺仍然有效：本次为显式注册）。'
    exit 0
} catch {
    Write-OpsBad ('注册过程出现意外错误：' + (Get-OpsSafeText -Text $_.Exception.Message))
    exit 1
}
