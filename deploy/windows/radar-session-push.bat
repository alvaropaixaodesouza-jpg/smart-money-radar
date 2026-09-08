@echo off
REM Sends a fomo session from your browser to the collector browser on the server.
REM
REM Run scripts/fomo_session_dump.js in your own Chrome first, on a fomo.family tab where you are
REM signed in. It downloads fomo-session.json. This sends that file over the same SSH key you
REM already use, hands it to the receiver on the server, and deletes the local copy.
REM
REM The value never appears on screen and never leaves the encrypted connection. The receiver
REM holds it for exactly one read by the collector extension and then deletes it.
REM
REM Pure ASCII on purpose - cmd reads .bat files in the ANSI codepage.

setlocal
set KEY=%USERPROFILE%\.ssh\fomoradar
set HOST=root@193.233.209.98
set FILE=%USERPROFILE%\Downloads\fomo-session.json

if not exist "%FILE%" (
  echo.
  echo   Could not find %FILE%
  echo.
  echo   Run scripts\fomo_session_dump.js in your browser's console on fomo.family first.
  echo.
  pause
  exit /b 1
)

echo.
echo   Sending the session to the server...
scp -i "%KEY%" "%FILE%" %HOST%:/tmp/fomo-session.json || goto :failed

ssh -i "%KEY%" %HOST% "bash -c 'T=$(sed -n \"s/^RECEIVER_TOKEN=//p\" /opt/fomoradar/app/.env | head -1); curl -s -X POST -H \"x-agent-token: $T\" -H \"content-type: application/json\" --data-binary @/tmp/fomo-session.json http://127.0.0.1:8787/seed; echo; rm -f /tmp/fomo-session.json; systemctl restart radar-browser'" || goto :failed

del "%FILE%"
echo.
echo   Sent, and both copies deleted. The collector picks it up within a minute.
echo.
pause
exit /b 0

:failed
echo.
echo   Failed. The file is still at %FILE% if you want to retry.
echo.
pause
exit /b 1
