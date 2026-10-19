@echo off
rem ===========================================================================
rem  xhs-agent one-click stop (double-click me)
rem  Internally runs: scripts\ops\stop.ps1  (see docs\operations.md)
rem  Optional args, e.g.:  stop.bat -DownContainers
rem  NOTE: containers alone are stopped without -DownContainers; data volumes
rem        are never removed (the -v flag is never used, by design).
rem ===========================================================================
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ops\stop.ps1" %*
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" (
  echo [xhs-agent] stop finished with exit code %RC%. Read the messages above.
) else (
  echo [xhs-agent] stop complete. Containers and data are kept by default.
)
echo.
echo Press any key to close this window...
pause >nul
exit /b %RC%
