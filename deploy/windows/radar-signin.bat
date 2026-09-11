@echo off
REM Opens a private door to the server's browser so you can sign into fomo.family once.
REM
REM Nothing is exposed to the internet: the remote desktop listens on the server's loopback only,
REM and this tunnel is the single way to reach it. Close this window and the door shuts.
REM
REM Pure ASCII on purpose - cmd reads .bat files in the ANSI codepage and mangles anything else.

setlocal
set KEY=%USERPROFILE%\.ssh\fomoradar
if "%RADAR_HOST%"=="" (
    echo set RADAR_HOST=root@your-server first
    exit /b 1
)
set HOST=%RADAR_HOST%

echo.
echo   FOMO Robinhood Radar - sign in to fomo.family on the server
echo   ------------------------------------------------------------
echo.
echo   1. Leave this window open.
echo   2. Open this address in your browser:
echo.
echo        http://127.0.0.1:6080/vnc.html?autoconnect=1^&resize=remote
echo.
echo   3. You will see the server's Chrome. Sign in to fomo.family as usual.
echo   4. Close this window when you are done. That is what stops the remote desktop.
echo.

REM The trap is what guarantees the door shuts: closing this window drops the connection, the
REM remote shell gets a hangup, and the servers stop even though the sleep never finished.
ssh -i "%KEY%" -L 6080:127.0.0.1:6080 %HOST% "bash -c 'trap \"systemctl stop radar-novnc radar-vnc\" EXIT HUP INT TERM; systemctl start radar-vnc radar-novnc; echo READY; sleep 7200'"

echo.
echo   Closed. The remote desktop on the server is stopped.
pause
