@echo off
rem ===========================================================================
rem  xhs-agent one-click start (double-click me)
rem  Internally runs: scripts\ops\start.ps1  (see docs\operations.md)
rem  Optional args, e.g.:  start.bat -Dev   /   start.bat -Watch
rem ===========================================================================
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ops\start.ps1" %*
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" (
  echo [xhs-agent] start failed, exit code %RC%. Read the messages above.
  echo [xhs-agent] Common fixes: see docs\operations.md  (troubleshooting table)
) else (
  echo [xhs-agent] startup complete. Backend :8000 / Frontend :3000 are running.
  echo [xhs-agent] To stop later, double-click stop.bat
)
echo.
echo Press any key to close this window...
pause >nul
exit /b %RC%
