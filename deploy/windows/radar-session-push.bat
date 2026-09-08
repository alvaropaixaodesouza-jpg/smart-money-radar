@echo off
REM Sends a fomo session from your browser to the collector browser on the server.
REM
REM First run fomo-session-dump.js in your own Chrome, on a fomo.family tab where you are signed
REM in: F12, Console, paste, Enter. It saves fomo-session.json. Then run this, or just drag that
REM file onto this file.
REM
REM The value never appears on screen and never leaves the encrypted connection. The receiver on
REM the server holds it for exactly one read by the collector and then deletes it.
REM
REM Pure ASCII on purpose - cmd reads .bat files in the ANSI codepage.

setlocal enabledelayedexpansion
set KEY=%USERPROFILE%\.ssh\fomoradar
set HOST=root@193.233.209.98
set FILE=

REM Dragged onto this file wins; otherwise look where a browser would have put it.
if not "%~1"=="" if exist "%~1" set FILE=%~1

if "!FILE!"=="" for %%D in (
  "%USERPROFILE%\Downloads"
  "%USERPROFILE%\Desktop"
  "%USERPROFILE%\OneDrive\Downloads"
  "%USERPROFILE%\OneDrive\Desktop"
  "%CD%"
) do (
  if exist "%%~D\fomo-session.json" if "!FILE!"=="" set FILE=%%~D\fomo-session.json
)

REM Chrome numbers a repeat download fomo-session (1).json, so take the newest match too.
if "!FILE!"=="" for /f "delims=" %%F in ('dir /b /o-d "%USERPROFILE%\Downloads\fomo-session*.json" 2^>nul') do (
  if "!FILE!"=="" set FILE=%USERPROFILE%\Downloads\%%F
)

if "!FILE!"=="" (
  echo.
  echo   No fomo-session.json anywhere obvious.
  echo.
  echo   Looked in: Downloads, Desktop, OneDrive versions of both, and this folder.
  echo.
  echo   To make one:
  echo     1. Open fomo.family in your own Chrome, signed in.
  echo     2. F12, then the Console tab.
  echo     3. Open fomo-session-dump.js from your Desktop in Notepad, copy all of it,
  echo        paste into the console, press Enter.
  echo     4. The console prints the key names it took. If it says "nothing to take",
  echo        you are on the wrong tab or not signed in on it.
  echo     5. Run this file again, or drag the downloaded file onto it.
  echo.
  pause
  exit /b 1
)

echo.
echo   Found !FILE!
echo   Sending to the server...
scp -i "%KEY%" "!FILE!" %HOST%:/tmp/fomo-session.json || goto :failed

ssh -i "%KEY%" %HOST% "bash -c 'T=$(sed -n \"s/^RECEIVER_TOKEN=//p\" /opt/fomoradar/app/.env | head -1); curl -s -X POST -H \"x-agent-token: $T\" -H \"content-type: application/json\" --data-binary @/tmp/fomo-session.json http://127.0.0.1:8787/seed; echo; rm -f /tmp/fomo-session.json; systemctl restart radar-browser'" || goto :failed

del "!FILE!"
echo.
echo   Sent, and both copies deleted. The collector picks it up within a minute.
echo.
pause
exit /b 0

:failed
echo.
echo   Failed. The file is still at !FILE! if you want to retry.
echo.
pause
exit /b 1
