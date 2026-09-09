@echo off
REM ===================================================================
REM  Zelda Switch 2 stock monitor - local runner
REM
REM  There is nothing to edit in this file.
REM
REM  Put your Discord webhook in a file called ".env" next to this one:
REM
REM      DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123/abc
REM
REM  Copy .env.example to .env to get started. Then double-click this
REM  file. Leave the window open - closing it stops the monitor.
REM ===================================================================

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERROR: Python isn't on your PATH.
    echo   Install it from python.org and tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo.
    echo   ERROR: No .env file found in this folder.
    echo.
    echo   Copy .env.example to .env, then open .env in Notepad and
    echo   paste your Discord webhook URL after DISCORD_WEBHOOK_URL=
    echo.
    pause
    exit /b 1
)

echo.
echo   Sending a test alert first...
echo.
python stock_monitor.py --test-alert
if errorlevel 1 (
    echo.
    echo   The test alert failed. Fix that before relying on this.
    echo   Check the DISCORD_WEBHOOK_URL line in your .env file.
    echo.
    pause
    exit /b 1
)

echo.
echo   Test alert sent. Starting the monitor - leave this window open.
echo   Press Ctrl+C to stop.
echo.
python stock_monitor.py --loop --interval 45
pause
