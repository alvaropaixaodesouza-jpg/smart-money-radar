@echo off
title FOMO Robinhood Radar - tunnel
rem Carries 127.0.0.1:8787 on this machine through to the receiver on the server.
rem The browser extension keeps posting to its own default address and never knows
rem the difference. Nothing is exposed to the internet: the tunnel is authenticated
rem by the deploy key, and the receiver behind it also wants the shared token.
:loop
echo.
echo [%date% %time%] connecting...
ssh -N -T ^
    -o ExitOnForwardFailure=yes ^
    -o ServerAliveInterval=30 ^
    -o ServerAliveCountMax=3 ^
    -o StrictHostKeyChecking=accept-new ^
    -o ConnectTimeout=15 ^
    -i "%USERPROFILE%\.ssh\fomoradar" ^
    -L 8787:127.0.0.1:8787 ^
    root@193.233.209.98
echo [%date% %time%] tunnel closed, retrying in 10s
timeout /t 10 /nobreak >nul
goto loop
