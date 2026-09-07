@echo off
title Enable tunnel autostart
echo.
echo   Registering the tunnel to start when you log in.
echo   It will run hidden, with no window.
echo.
schtasks /Create /TN "FOMO Radar tunnel" /TR "wscript.exe \"%~dp0radar-tunnel-hidden.vbs\"" /SC ONLOGON /RL LIMITED /F
echo.
echo   Done. To remove it later:
echo     schtasks /Delete /TN "FOMO Radar tunnel" /F
echo.
pause
