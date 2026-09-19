<#
===============================================================================
 Register-GamerFriendsSyncTask.ps1

 SCRIPT HIGHLIGHTS
 - Registers (or removes) the "Gamer-Friends-Sync" scheduled task that runs
   run_gamer_friends_sync.bat once a day.
 - Runs in your user context via S4U, so it fires whether or not you are logged
   on and needs no stored password. Your user environment variables and
   secrets.local.json are available to the task.
 - -RunNow triggers the task immediately for a live test. -Unregister removes it.

 REQUIREMENTS
 - Windows PowerShell 5.1 or newer, run as the user who owns the credentials.
 - run_gamer_friends_sync.bat and gamer_friends_sync.py in the same folder as
   this script (the default), or pass -BatchPath explicitly.

 DISCLAIMER
 - This only creates a local scheduled task that runs a read-only export. It
   makes no changes to your gaming accounts. Removing the task (-Unregister)
   leaves your scripts and exported data untouched.

 Last Updated: 2026-09-18
===============================================================================
#>

#Requires -Version 5.1
[CmdletBinding()]
param(
    # Full path to the batch wrapper. Defaults to the one beside this script.
    [string]$BatchPath = (Join-Path $PSScriptRoot 'run_gamer_friends_sync.bat'),

    # Daily run time, 24-hour "HH:mm". Default 08:00 local.
    [string]$Time = '08:00',

    # Trigger the task once, right now, after (or instead of) registering.
    [switch]$RunNow,

    # Remove the task instead of creating it.
    [switch]$Unregister
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TaskName = 'Gamer-Friends-Sync'

function Write-Banner {
    param([string]$Text, [string]$Color = 'Cyan')
    Write-Host ''
    Write-Host "==== $Text ====" -ForegroundColor $Color
}

try {
    if ($Unregister) {
        Write-Banner "Removing scheduled task '$TaskName'" 'Yellow'
        $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $existing) {
            Write-Host "Task '$TaskName' does not exist. Nothing to remove." -ForegroundColor Yellow
            exit 0
        }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed '$TaskName'." -ForegroundColor Green
        exit 0
    }

    Write-Banner "Registering scheduled task '$TaskName'"

    # Validate inputs before touching the scheduler.
    if (-not (Test-Path -LiteralPath $BatchPath)) {
        throw "Batch wrapper not found at '$BatchPath'. Pass -BatchPath with the correct location."
    }
    try {
        $parsedTime = [datetime]::ParseExact($Time, 'HH:mm', $null)
    } catch {
        throw "Time '$Time' is not valid. Use 24-hour HH:mm, for example 08:00 or 17:30."
    }

    $workingDir = Split-Path -Parent $BatchPath

    # Run the .bat through cmd.exe /c so exit codes propagate cleanly.
    $action = New-ScheduledTaskAction -Execute 'cmd.exe' `
        -Argument ('/c "{0}"' -f $BatchPath) `
        -WorkingDirectory $workingDir

    $trigger = New-ScheduledTaskTrigger -Daily -At $parsedTime

    # S4U: run as this user whether logged on or not, no stored password.
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType S4U -RunLevel Limited

    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null

    Write-Host "Registered '$TaskName' to run daily at $Time." -ForegroundColor Green
    Write-Host "Runs: cmd.exe /c `"$BatchPath`"" -ForegroundColor Gray

    # Verify the task is actually present rather than trusting the create call.
    $verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $verify) {
        throw "Registration reported success but '$TaskName' is not present. Check permissions."
    }

    if ($RunNow) {
        Write-Banner "Starting '$TaskName' now for a live test"
        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 3
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host ("Last run time:   {0}" -f $info.LastRunTime) -ForegroundColor Gray
        Write-Host ("Last run result: {0}" -f $info.LastTaskResult) -ForegroundColor Gray
        Write-Host "See output\run.log next to the script for details." -ForegroundColor Gray
    }

    exit 0
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
