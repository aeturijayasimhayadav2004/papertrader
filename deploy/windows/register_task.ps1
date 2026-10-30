<#
Windows fallback for testing PaperTrader's intraday PAPER loop before the Oracle VM exists.
Registers a Task Scheduler task that runs "scripts\intraday_loop.py --once" every 5 minutes,
Monday-Friday, 09:15-15:30 IST, while you are logged on. PAPER TRADING ONLY.

    powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1
    powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1 -Unregister

The PC must be on and awake. The times are converted from IST to this PC's time zone.
Check it:  Get-ScheduledTask PaperTraderIntraday | Get-ScheduledTaskInfo
Run now:   Start-ScheduledTask PaperTraderIntraday
#>
param(
    [string]$TaskName = "PaperTraderIntraday",
    [switch]$DryRun,
    [switch]$Unregister
)
$ErrorActionPreference = "Stop"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task $TaskName."
    return
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "No venv at $Python. Create it first (python -m venv .venv; pip install -r requirements.txt)." }
$Script = Join-Path $Root "scripts\intraday_loop.py"
$ArgList = "`"$Script`" --once --quiet"
if ($DryRun) { $ArgList += " --dry-run" }

# 09:15:30 IST in this PC's local time (the same clock time if the PC is set to IST).
$Ist = [System.TimeZoneInfo]::FindSystemTimeZoneById("India Standard Time")
$IstStart = [datetime]::SpecifyKind((Get-Date).Date.AddHours(9).AddMinutes(15).AddSeconds(30), "Unspecified")
$LocalStart = [System.TimeZoneInfo]::ConvertTime($IstStart, $Ist, [System.TimeZoneInfo]::Local)

$Action = New-ScheduledTaskAction -Execute $Python -Argument $ArgList -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $LocalStart
# Repeat every 5 minutes for 6 h 15 min: 09:15:30 .. 15:30:30 IST.
$Repeat = New-ScheduledTaskTrigger -Once -At $LocalStart -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Hours 6 -Minutes 15)
$Trigger.Repetition = $Repeat.Repetition
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) `
    -StartWhenAvailable:$false -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "PaperTrader intraday PAPER cycle every 5 min, Mon-Fri 09:15-15:30 IST (paper money only)" -Force | Out-Null
Write-Host "Registered ${TaskName}: $Python $ArgList"
Write-Host "Starts $($LocalStart.ToString('HH:mm:ss')) local time (09:15:30 IST), every 5 minutes until 15:30 IST, Mon-Fri."
Write-Host "Logs: $Root\logs\papertrader.log   Status: .venv\Scripts\python scripts\intraday_loop.py --status"
