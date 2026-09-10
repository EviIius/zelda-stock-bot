$ErrorActionPreference = "Continue"
$repoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $repoDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Set-Location -LiteralPath $repoDir
$env:PYTHONUTF8 = "1"

# Stop only a previously recorded monitor child. Windows Task Scheduler may
# terminate this wrapper without terminating the Python process it launched.
$heartbeatFile = Join-Path $repoDir "runtime_heartbeat.json"
if (Test-Path -LiteralPath $heartbeatFile) {
    try {
        $oldPid = [int](Get-Content -LiteralPath $heartbeatFile -Raw | ConvertFrom-Json).pid
        $oldProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $oldPid"
        if ($oldProcess -and $oldProcess.Name -eq "python.exe" -and
                $oldProcess.CommandLine -match "stock_monitor\.py\s+--loop") {
            Stop-Process -Id $oldPid -Force
            "[{0}] stopped orphan monitor PID {1}" -f (Get-Date -Format o), $oldPid |
                Tee-Object -FilePath (Join-Path $logDir ("monitor-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))) -Append
        }
    } catch {
        # A stale/malformed heartbeat must never prevent startup.
    }
}

Get-ChildItem -LiteralPath $logDir -Filter "monitor-*.log" -File |
    Where-Object LastWriteTime -lt (Get-Date).AddDays(-14) |
    Remove-Item -Force

while ($true) {
    $logFile = Join-Path $logDir ("monitor-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    "[{0}] starting monitor" -f (Get-Date -Format o) | Tee-Object -FilePath $logFile -Append
    & python -u stock_monitor.py --loop 2>&1 | Tee-Object -FilePath $logFile -Append
    $exitCode = $LASTEXITCODE
    "[{0}] monitor exited with code {1}; restarting in 10 seconds" -f (Get-Date -Format o), $exitCode |
        Tee-Object -FilePath $logFile -Append
    Start-Sleep -Seconds 10
}
