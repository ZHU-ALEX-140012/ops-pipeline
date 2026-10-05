@echo off
rem Double-click friendly wrapper for set-secret.ps1 (generic credential writer).
rem
rem Runs with -ExecutionPolicy Bypass for THIS process only; no admin, no system change.
rem
rem Usage:
rem   set-secret.cmd -List
rem   set-secret.cmd -Name OPS_NTFY_URL -Hint "https://ntfy.sh/your-topic"
rem   set-secret.cmd -Name OPS_MAIL_PASS -Masked
rem   set-secret.cmd -Name OPS_NTFY_URL -Remove
rem
rem ASCII-only on purpose: cmd.exe reads .cmd files in the OEM code page.

setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set-secret.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

if "%~1"=="" pause
exit /b %EXITCODE%
