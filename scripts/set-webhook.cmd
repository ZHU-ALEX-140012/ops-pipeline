@echo off
rem Double-click friendly wrapper for set-webhook.ps1.
rem
rem The default PowerShell execution policy blocks unsigned .ps1 files, so this
rem runs the script with -ExecutionPolicy Bypass for THIS process only -- it does
rem not change any system-wide setting and needs no admin rights.
rem
rem Usage:
rem   set-webhook.cmd            prompt for the webhook, save it, send a test message
rem   set-webhook.cmd -Show      show current status (key is masked)
rem   set-webhook.cmd -Remove    remove it
rem
rem ASCII-only on purpose: cmd.exe reads .cmd files in the OEM code page.

setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set-webhook.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

rem Pause only when double-clicked (no arguments).
if "%~1"=="" pause
exit /b %EXITCODE%
