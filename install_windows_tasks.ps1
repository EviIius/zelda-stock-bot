$ErrorActionPreference = "Stop"
$repoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$monitorScript = Join-Path $repoDir "stock_monitor.py"
$watchdogScript = Join-Path $repoDir "watchdog.py"
$uiScript = Join-Path $repoDir "stock_bot_ui.py"
$python = (Get-Command python).Source
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw)) {
    $pythonw = $python
}
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$logDir = Join-Path $repoDir "logs"
$monitorLog = Join-Path $logDir "windows-monitor.log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

& $python -c "import sys; raise SystemExit(sys.version_info < (3, 11))"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.11 or newer is required."
}
& $python -m pip install -r (Join-Path $repoDir "requirements.txt") `
    -r (Join-Path $repoDir "requirements-browser.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Python dependency installation failed."
}
& $python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    throw "Playwright Chromium installation failed."
}
& $python -m unittest discover -s $repoDir -p "test_*.py"
if ($LASTEXITCODE -ne 0) {
    throw "Project self-tests failed; scheduled tasks were not installed."
}

$monitorAction = New-ScheduledTaskAction -Execute $pythonw -Argument (
    '-u "{0}" --loop --service-log "{1}"' -f $monitorScript, $monitorLog
) -WorkingDirectory $repoDir
$monitorTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$monitorSettings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable `
    -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Zelda Stock Monitor" -Action $monitorAction -Trigger $monitorTrigger `
    -Settings $monitorSettings -Description "Independent real-time retailer stock workers" -Force | Out-Null

$watchdogAction = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}"' -f $watchdogScript) `
    -WorkingDirectory $repoDir
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 2)
$watchdogSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Zelda Stock Monitor Watchdog" -Action $watchdogAction `
    -Trigger $watchdogTrigger -Settings $watchdogSettings `
    -Description "Alerts when the Zelda stock monitor heartbeat stops" -Force | Out-Null

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Zelda Stock Bot.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = ('"{0}"' -f $uiScript)
$shortcut.WorkingDirectory = $repoDir
$shortcut.Description = "Open the Zelda Stock Bot control panel"
$shortcut.Save()

Start-ScheduledTask -TaskName "Zelda Stock Monitor"
Write-Host "Installed and started 'Zelda Stock Monitor'."
Write-Host "Installed 'Zelda Stock Monitor Watchdog' (runs every two minutes)."
Write-Host "The monitor runs directly under Task Scheduler with no console window."
Write-Host "Created desktop shortcut 'Zelda Stock Bot'."
