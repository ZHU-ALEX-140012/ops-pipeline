@echo off
rem 只读状态面板：双击我就能打开。
rem
rem 为什么要有这个文件：面板是个前台服务，关掉窗口它就停了。
rem 想看的时候双击一下，比记命令或找人帮你起都省事。
setlocal
set "OPS_ROOT=%~dp0.."
cd /d "%OPS_ROOT%" || exit /b 1

rem ------------------------------------------------------------------ 找 Python
rem 优先复用计划任务已经确认过能跑的那个解释器（register_tasks.ps1 把绝对路径
rem 写进了 run_once.cmd）。这样"面板能不能跑"和"计划任务能不能跑"用的是同一个
rem Python，不会出现"定时任务好的、手动点开报错"这种怪事。
rem 找不到才退回 PATH 里的 python。
set "PYTHON="
for /f "usebackq tokens=* delims=" %%L in (`findstr /r /c:"python.exe" "scripts\run_once.cmd" 2^>nul`) do (
    for /f "tokens=1 delims= " %%P in ("%%L") do set "PYTHON=%%~P"
)
if not defined PYTHON set "PYTHON=python"

echo.
echo   ops-pipeline 只读状态面板
echo   地址： http://127.0.0.1:8899/
echo.
echo   用浏览器打开上面那个地址。关掉这个窗口就停止。
echo   解释器： %PYTHON%
echo.

"%PYTHON%" tools\dashboard.py --port 8899
echo.
echo   面板已停止。
pause
