#Requires -Version 5.1
<#
.SYNOPSIS
    xhs-agent · 撤销「每日定时备份」（删除计划任务，删除即复原）

.DESCRIPTION
    与 install-backup-task.ps1 配对：删除 xhs-agent\backup 计划任务（若存在），
    回读确认已消失，并尝试清理空的 xhs-agent 任务文件夹。
    未注册时运行本脚本为安全空操作（退出码 0）。
    ⚠️ 只删除「定时触发」这一层；已生成的 backups\ 备份文件不会被删除。

    典型用法：
        powershell -ExecutionPolicy Bypass -File scripts\ops\uninstall-backup-task.ps1

    退出码约定：0 = 已撤销（或本就未注册）；1 = 撤销失败
#>
[CmdletBinding()]
param([string]$Root = '')

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ops-common.ps1')
Initialize-OpsConsole

try {
    $Root = Resolve-OpsRoot -ScriptRoot $PSScriptRoot -Override $Root
    $taskName = 'xhs-agent-backup'

    Write-Host '=================================================='
    Write-Host ' xhs-agent · 撤销「每日定时备份」'
    Write-Host '=================================================='

    $existing = Get-OpsScheduledTask -TaskName $taskName
    if ($null -eq $existing) {
        Write-OpsInfo '未注册该计划任务（xhs-agent\backup 不存在）—— 无需撤销。'
        Remove-OpsTaskFolderQuiet
        Write-OpsOk '确认无残留。'
        exit 0
    }

    Write-OpsStep '删除计划任务'
    $ok = Unregister-OpsScheduledTask -TaskName $taskName
    if (-not $ok) {
        Write-OpsBad '撤销失败（见上方原因）。'
        exit 1
    }
    $after = Get-OpsScheduledTask -TaskName $taskName
    if ($null -ne $after) {
        Write-OpsBad '删除后回读仍存在，撤销未完成。'
        exit 1
    }
    Write-OpsOk '计划任务已删除（回读确认：无残留）。'
    Write-OpsInfo '已生成的 backups\ 备份文件保留（本脚本不删除任何备份）。'
    exit 0
} catch {
    Write-OpsBad ('撤销过程出现意外错误：' + (Get-OpsSafeText -Text $_.Exception.Message))
    exit 1
}
