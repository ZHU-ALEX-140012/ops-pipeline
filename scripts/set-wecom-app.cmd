@echo off
rem Double-click friendly wrapper for set-wecom-app.ps1.
rem
rem Runs the script with -ExecutionPolicy Bypass for THIS process only (the default
rem policy blocks unsigned .ps1 files). No admin rights, no system-wide changes.
rem
rem Usage:
rem   set-wecom-app.cmd            prompt for CorpID/Secret/AgentId, save, send a test
rem   set-wecom-app.cmd -Show      show current status (values masked)
rem   set-wecom-app.cmd -Remove    remove the three values
rem
rem ASCII-only on purpose: cmd.exe reads .cmd files in the OEM code page.

setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set-wecom-app.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

rem Pause only when double-clicked (no arguments).
if "%~1"=="" pause
exit /b %EXITCODE%
