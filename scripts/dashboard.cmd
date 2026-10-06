@echo off
rem ops-pipeline read-only dashboard: double-click me to open it.
rem
rem Why this file exists: the dashboard is a foreground service, so it stops
rem when its window closes. Double-clicking is easier than remembering a command.
rem
rem Two conventions this file follows (both are enforced by tests):
rem   1) ASCII ONLY. A .cmd with multi-byte characters breaks cmd.exe parsing --
rem      and putting `chcp 65001` inside the file makes it worse, because changing
rem      the code page mid-file corrupts cmd's own read position. All user-facing
rem      Chinese comes from the Python side instead.
rem   2) Prefer the interpreter that run_once.cmd already uses (the one the
rem      scheduled task has proven works), so "scheduled task is fine but manual
rem      launch errors" cannot happen. Fall back to python on PATH.

setlocal
set "OPS_ROOT=%~dp0.."
cd /d "%OPS_ROOT%" || exit /b 1

set "PYTHON="
for /f "usebackq tokens=1 delims= " %%P in (`findstr /r /c:"python.exe" "scripts\run_once.cmd" 2^>nul`) do (
    if not defined PYTHON set "PYTHON=%%~P"
)
if not defined PYTHON set "PYTHON=python"

"%PYTHON%" tools\dashboard.py --port 8899

echo.
echo   Dashboard stopped.
pause
