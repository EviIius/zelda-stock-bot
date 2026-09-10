$ErrorActionPreference = "Stop"
$repoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$serviceScript = Join-Path $repoDir "run_service.ps1"
$watchdogScript = Join-Path $repoDir "watchdog.py"
$python = (Get-Command python).Source
$powershell = (Get-Command powershell.exe).Source
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$monitorAction = New-ScheduledTaskAction -Execute $powershell -Argument (
    '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $serviceScript
)
$monitorTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$monitorSettings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable `
    -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Zelda Stock Monitor" -Action $monitorAction -Trigger $monitorTrigger `
    -Settings $monitorSettings -Description "Independent real-time retailer stock workers" -Force | Out-Null

$watchdogAction = New-ScheduledTaskAction -Execute $python -Argument ('"{0}"' -f $watchdogScript)
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 2)
$watchdogSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Zelda Stock Monitor Watchdog" -Action $watchdogAction `
    -Trigger $watchdogTrigger -Settings $watchdogSettings `
    -Description "Alerts when the Zelda stock monitor heartbeat stops" -Force | Out-Null

Start-ScheduledTask -TaskName "Zelda Stock Monitor"
Write-Host "Installed and started 'Zelda Stock Monitor'."
Write-Host "Installed 'Zelda Stock Monitor Watchdog' (runs every two minutes)."
