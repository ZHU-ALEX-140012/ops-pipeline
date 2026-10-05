<#
.SYNOPSIS
    注册 ops-pipeline 的 Windows 计划任务（D1 决策的落地）。

.DESCRIPTION
    做三件事：
    1. 用检测到的 Python 绝对路径生成 scripts\run_once.cmd
       （计划任务的环境变量不保证和你的终端一致，所以路径必须写死）；
    2. 注册两个任务：
         OpsPipeline-ApiWatch  每小时一次，跑接口巡检
         OpsPipeline-Heartbeat 每天 23:00，检查"预期该跑的 run 有没有跑"
    3. 打印注册结果，便于核对。

    默认任务在"你已登录"时运行。加 -RunWhenLoggedOff 改成就绪即跑（S4U，不存密码），
    但这种模式只能访问本机资源；如果你用到需要网络凭据的东西，请先实测。

.EXAMPLE
    # 先看看会做什么，不改系统
    .\register_tasks.ps1 -DryRun

.EXAMPLE
    # 正式注册
    .\register_tasks.ps1

.EXAMPLE
    # 立刻跑一次验证
    Start-ScheduledTask -TaskName OpsPipeline-ApiWatch

.EXAMPLE
    # 全部移除
    .\register_tasks.ps1 -Remove
#>
[CmdletBinding()]
param(
    # 留空则在本脚本体内推导：$PSScriptRoot 在 param 默认值里不保证已赋值
    [string]$ProjectRoot,
    [string]$PythonPath,
    [string]$ConfigPath = "config/config.toml",
    [switch]$RunWhenLoggedOff,
    [switch]$Remove,
    [switch]$DryRun,
    [switch]$IncludeLogScan
)

$ErrorActionPreference = 'Stop'

if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }

$apiTaskName = 'OpsPipeline-ApiWatch'
$hbTaskName  = 'OpsPipeline-Heartbeat'
$regTaskName = 'OpsPipeline-Regression'
$logTaskName = 'OpsPipeline-LogScan'
$dlyTaskName = 'OpsPipeline-DailyReport'

# 默认注册四个任务：接口巡检、回归、日报、心跳。它们的输入都在项目内，装好就能跑。
#
# log_scan 为什么默认不注册：它监视 `[[log_scan.sources]]` 指向的日志，
# 而仓库里的默认值是**演示用的合成日志**（tools/loggen.py 生成）。
# 真注册了它，日志一停止增长就会持续报"日志停止写入"——对演示文件而言那是误导性告警。
# 把它指向自己的真实日志后，用 -IncludeLogScan 一并注册。
#
# 日报默认注册：它汇总"已经注册的那些任务"的运行情况，
# 不管有没有异常都发一份，让"今天一切正常"也成为明确信息。
$taskNames = @($apiTaskName, $hbTaskName, $regTaskName, $dlyTaskName)
if ($IncludeLogScan) { $taskNames += $logTaskName }

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Invoke-Or-Show([scriptblock]$action, [string]$description) {
    if ($DryRun) { Write-Host "    [DryRun] $description" -ForegroundColor Yellow }
    else { & $action }
}

# ---------------------------------------------------------------- 移除模式
if ($Remove) {
    foreach ($name in $taskNames) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($existing) {
            Invoke-Or-Show { Unregister-ScheduledTask -TaskName $name -Confirm:$false } "移除计划任务 $name"
            Write-Host "    已移除 $name"
        } else {
            Write-Host "    未找到 $name（跳过）"
        }
    }
    return
}

# ---------------------------------------------------------------- 前置检查
Write-Step "检查项目与解释器"
if (-not (Test-Path -LiteralPath $ProjectRoot)) { throw "项目目录不存在: $ProjectRoot" }
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

if (-not $PythonPath) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "找不到 python，请用 -PythonPath 指定绝对路径" }
    $PythonPath = $cmd.Source
}
if (-not (Test-Path -LiteralPath $PythonPath)) { throw "Python 路径不存在: $PythonPath" }
Write-Host "    项目根目录 : $ProjectRoot"
Write-Host "    解释器     : $PythonPath"

if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $ConfigPath))) {
    Write-Warning "找不到配置文件 $ConfigPath（任务仍会注册，但运行时可能报配置错误）"
}

# ---------------------------------------------------------------- 生成启动包装
Write-Step "生成 scripts\run_once.cmd"
$scriptsDir = Join-Path $ProjectRoot 'scripts'
if (-not (Test-Path -LiteralPath $scriptsDir)) { New-Item -ItemType Directory -Path $scriptsDir | Out-Null }
$launcher = Join-Path $scriptsDir 'run_once.cmd'

# 用单引号 here-string，避免 PowerShell 展开 cmd 的 %1
$launcherContent = @'
@echo off
rem 由 register_tasks.ps1 生成，请勿手改（重新运行脚本会覆盖）。
rem 作用：固定工作目录与解释器路径，把启动层输出落盘。
rem 详细日志由 Python 自己写到 data\logs\ 与 SQLite。
setlocal
set "OPS_ROOT=__PROJECT_ROOT__"
cd /d "%OPS_ROOT%" || exit /b 1
if not exist "data\logs" mkdir "data\logs"
"__PYTHON__" -m ops --task %1 --once --config "__CONFIG__" >> "data\logs\launcher.log" 2>&1
exit /b %errorlevel%
'@
$launcherContent = $launcherContent.Replace('__PROJECT_ROOT__', $ProjectRoot).Replace('__PYTHON__', $PythonPath).Replace('__CONFIG__', (Join-Path $ProjectRoot $ConfigPath))

if ($DryRun) {
    Write-Host "    [DryRun] 将写入 $launcher" -ForegroundColor Yellow
} else {
    Set-Content -LiteralPath $launcher -Value $launcherContent -Encoding ASCII
    Write-Host "    已写入 $launcher"
}

# ---------------------------------------------------------------- 任务定义
$logonType = if ($RunWhenLoggedOff) { 'S4U' } else { 'Interactive' }
$userId = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
Write-Host "    运行身份   : $userId ($logonType)"
if ($RunWhenLoggedOff) {
    Write-Warning "S4U 模式下任务只能访问本机资源；请先用 Start-ScheduledTask 实测一次。"
}

# 预览模式到此为止：下面每一步都要访问系统的任务计划服务（CIM），
# 预览就该完全不碰系统 —— 也让受沙箱限制的环境能安全地跑 -DryRun。
if ($DryRun) {
    Write-Host ""
    Write-Step "预览：将会注册以下计划任务（未改动系统）"
    Write-Host "    $apiTaskName"
    Write-Host "        触发：从现在起每小时（首次 $((Get-Date).AddMinutes(2).ToString('HH:mm'))）"
    Write-Host "        执行：`"$launcher`" api_watch"
    Write-Host "    $hbTaskName"
    Write-Host "        触发：每天 23:00"
    Write-Host "        执行：`"$launcher`" heartbeat"
    Write-Host "    $regTaskName"
    Write-Host "        触发：每天 08:30"
    Write-Host "        执行：`"$launcher`" regression"
    Write-Host "    $dlyTaskName"
    Write-Host "        触发：每天 09:30"
    Write-Host "        执行：`"$launcher`" daily_report"
    if ($IncludeLogScan) {
        Write-Host "    $logTaskName"
        Write-Host "        触发：每天 09:00"
        Write-Host "        执行：`"$launcher`" log_scan"
    } else {
        Write-Host "    （未含 $logTaskName）" -ForegroundColor DarkGray
        Write-Host "        log_scan 默认不注册：它监视的日志源还是演示用的合成日志。" -ForegroundColor DarkGray
        Write-Host "        把它指向你自己的日志后，加 -IncludeLogScan 一并注册。" -ForegroundColor DarkGray
    }
    Write-Host "    附加设置：错过则补跑 / 允许电池供电 / 不并发 / 超时自动结束"
    Write-Host ""
    Write-Host "    确认无误后去掉 -DryRun 重新运行即可。" -ForegroundColor Green
    return
}

$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType $logonType -RunLevel Limited

# 每小时：从 2 分钟后开始，之后每小时重复（重复时长取 10 年 ≈ 无限）
$startAt = (Get-Date).AddMinutes(2)
$hourlyTrigger = New-ScheduledTaskTrigger -Once -At $startAt `
    -RepetitionInterval (New-TimeSpan -Minutes 60) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At '23:00'
# 回归晨报放在早上 8:30、日志简报 9:00：你起床时报告已经躺在微信/报告目录里了
$morningTrigger = New-ScheduledTaskTrigger -Daily -At '08:30'
$logTrigger = New-ScheduledTaskTrigger -Daily -At '09:00'
# 日报放在最后：等回归（08:30）和日志分析（09:00）都跑完，汇总才有意义
$digestTrigger = New-ScheduledTaskTrigger -Daily -At '09:30'

# 执行时限必须小于触发间隔，否则一次卡死会不断堆积
$apiSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 50)
$hbSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$regSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$logSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$dlySettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$apiAction = New-ScheduledTaskAction -Execute $launcher -Argument 'api_watch' -WorkingDirectory $ProjectRoot
$hbAction  = New-ScheduledTaskAction -Execute $launcher -Argument 'heartbeat' -WorkingDirectory $ProjectRoot
$regAction = New-ScheduledTaskAction -Execute $launcher -Argument 'regression' -WorkingDirectory $ProjectRoot
$logAction = New-ScheduledTaskAction -Execute $launcher -Argument 'log_scan' -WorkingDirectory $ProjectRoot
$dlyAction = New-ScheduledTaskAction -Execute $launcher -Argument 'daily_report' -WorkingDirectory $ProjectRoot

# ---------------------------------------------------------------- 注册
Write-Step "注册计划任务"
Invoke-Or-Show {
    Register-ScheduledTask -TaskName $apiTaskName -Action $apiAction -Trigger $hourlyTrigger `
        -Settings $apiSettings -Principal $principal -Force `
        -Description 'ops-pipeline: 每小时接口可用性巡检' | Out-Null
} "注册 $apiTaskName（每小时）"

Invoke-Or-Show {
    Register-ScheduledTask -TaskName $hbTaskName -Action $hbAction -Trigger $dailyTrigger `
        -Settings $hbSettings -Principal $principal -Force `
        -Description 'ops-pipeline: 每日心跳，检查预期运行是否缺失' | Out-Null
} "注册 $hbTaskName（每天 23:00）"

Invoke-Or-Show {
    Register-ScheduledTask -TaskName $regTaskName -Action $regAction -Trigger $morningTrigger `
        -Settings $regSettings -Principal $principal -Force `
        -Description 'ops-pipeline: 每日接口回归 + AI 失败归因 + 晨报' | Out-Null
} "注册 $regTaskName（每天 08:30）"

if ($IncludeLogScan) {
    Invoke-Or-Show {
        Register-ScheduledTask -TaskName $logTaskName -Action $logAction -Trigger $logTrigger `
            -Settings $logSettings -Principal $principal -Force `
            -Description 'ops-pipeline: 每日日志异常分析 + AI 归因' | Out-Null
    } "注册 $logTaskName（每天 09:00）"
}

Invoke-Or-Show {
    Register-ScheduledTask -TaskName $dlyTaskName -Action $dlyAction -Trigger $digestTrigger `
        -Settings $dlySettings -Principal $principal -Force `
        -Description 'ops-pipeline: 每日日报（汇总运行/告警/成本）' | Out-Null
} "注册 $dlyTaskName（每天 09:30）"

# ---------------------------------------------------------------- 核对
Write-Step "核对结果"
if (-not $DryRun) {
    Get-ScheduledTask -TaskName $taskNames |
        Select-Object TaskName, State, @{n = 'NextRun'; e = { (Get-ScheduledTaskInfo -TaskName $_.TaskName).NextRunTime } } |
        Format-Table -AutoSize
    Write-Host ""
    Write-Host "手动验证一次：" -ForegroundColor Green
    Write-Host "    Start-ScheduledTask -TaskName $apiTaskName"
    Write-Host "    Get-Content `"$ProjectRoot\data\logs\launcher.log`" -Tail 20"
    Write-Host "    python -m ops --task heartbeat --once    # 检查心跳"
} else {
    Write-Host "    [DryRun] 未对系统做任何修改。" -ForegroundColor Yellow
}
