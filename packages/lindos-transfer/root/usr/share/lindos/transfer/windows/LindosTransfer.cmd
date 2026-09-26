@echo off
rem Lindos Transfer kit: double-click me (see README.txt). Only copies; changes nothing here.
rem Bypass applies to this one run only: Windows blocks unsigned scripts by default.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0LindosTransfer.ps1" %*
set "LINDOS_TRANSFER_RC=%ERRORLEVEL%"
echo.
pause
exit /b %LINDOS_TRANSFER_RC%
