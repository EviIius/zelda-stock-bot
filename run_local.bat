@echo off
setlocal enabledelayedexpansion

REM ===================================================================
REM  Zelda Switch 2 stock monitor - local runner
REM
REM  Paste your Discord webhook URL between the = and the closing quote
REM  on the line below. Do NOT add quotes of your own around the URL --
REM  in a .bat file, quotes inside the value become part of the value.
REM
REM     RIGHT:  set "DISCORD_WEBHOOK_URL=https://discord.com/api/..."
REM     WRONG:  set DISCORD_WEBHOOK_URL="https://discord.com/api/..."
REM
REM  Then just double-click this file. Leave the window open --
REM  closing it stops the monitor.
REM ===================================================================

set "DISCORD_WEBHOOK_URL=PASTE_YOUR_WEBHOOK_HERE"

REM --- Optional upgrades: remove the REM and fill in to enable --------
REM set "PUSHOVER_TOKEN="
REM set "PUSHOVER_USER="

REM --- Tuning --------------------------------------------------------
set "INTERVAL=45"

REM ===================================================================
REM  Nothing below here needs editing.
REM ===================================================================

cd /d "%~dp0"

REM Strip any quotes the user pasted anyway, so it works either way.
set "DISCORD_WEBHOOK_URL=!DISCORD_WEBHOOK_URL:"=!"

if "!DISCORD_WEBHOOK_URL!"=="PASTE_YOUR_WEBHOOK_HERE" (
    echo.
    echo   ERROR: You haven't pasted your Discord webhook URL yet.
    echo   Open this file in Notepad and edit the DISCORD_WEBHOOK_URL line.
    echo.
    pause
    exit /b 1
)

echo !DISCORD_WEBHOOK_URL! | findstr /b /c:"https://discord.com/api/webhooks/" >nul
if errorlevel 1 (
    echo !DISCORD_WEBHOOK_URL! | findstr /b /c:"https://discordapp.com/api/webhooks/" >nul
)
if errorlevel 1 (
    echo.
    echo   WARNING: That doesn't look like a Discord webhook URL.
    echo   Expected it to start with https://discord.com/api/webhooks/
    echo   Got: !DISCORD_WEBHOOK_URL!
    echo.
    echo   Press Ctrl+C to stop, or any key to try anyway.
    pause >nul
)

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERROR: Python isn't on your PATH.
    echo   Install it from python.org and tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo.
echo   Sending a test alert first...
python stock_monitor.py --test-alert
if errorlevel 1 (
    echo.
    echo   The test alert failed - fix that before relying on this.
    pause
    exit /b 1
)

echo.
echo   Test alert sent. Starting the monitor - leave this window open.
echo   Press Ctrl+C to stop.
echo.
python stock_monitor.py --loop --interval !INTERVAL!
pause
