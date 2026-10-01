@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
set "exitCode=%ERRORLEVEL%"
if not "%exitCode%"=="0" (
  echo.
  echo FINTRACE startup failed with exit code %exitCode%.
) else (
  echo.
  echo Services will keep running after this window closes.
)
pause
exit /b %exitCode%