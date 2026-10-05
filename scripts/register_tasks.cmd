@echo off
rem Double-click friendly wrapper for register_tasks.ps1.
rem
rem Why a wrapper: the default PowerShell execution policy blocks unsigned .ps1
rem files, so users hit "not digitally signed" errors. This runs the script with
rem -ExecutionPolicy Bypass for THIS process only -- it does not change any
rem system-wide setting and does not require an elevated prompt.
rem
rem Usage:
rem   register_tasks.cmd -DryRun     preview only, changes nothing
rem   register_tasks.cmd             register the scheduled tasks
rem   register_tasks.cmd -Remove     unregister them
rem
rem The file is kept ASCII-only because cmd.exe reads .cmd files in the OEM code
rem page and would garble UTF-8 text.

setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_tasks.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

rem Pause only when invoked with no arguments (i.e. double-clicked).
if "%~1"=="" pause
exit /b %EXITCODE%
