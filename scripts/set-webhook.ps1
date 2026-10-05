<#
.SYNOPSIS
    配置企业微信告警通道（写入本地密钥文件）。

.DESCRIPTION
    把群机器人 webhook 写进 config\secrets.local.toml。该文件已在 .gitignore 中，
    不会被提交。

    为什么用文件而不是环境变量：**计划任务不一定能及时看到新设的环境变量** ——
    Task Scheduler 服务的环境块可能被缓存，改完要重新登录才生效。
    文件是每次进程启动都现读的，没有这个不确定性。
    （两种方式都支持：环境变量优先，文件兜底。）

    怎么拿到 webhook：
        企业微信 → 打开任意群 → 右上角 … → 群机器人 → 添加机器人 → 复制 Webhook 地址
        个人也能注册企业微信，建一个只有自己的群用来收告警。

.EXAMPLE
    .\set-webhook.ps1                      # 交互式输入，写完立刻发一条测试消息
.EXAMPLE
    .\set-webhook.ps1 -Show                # 只看当前配置状态（不打印完整密钥）
.EXAMPLE
    .\set-webhook.ps1 -Remove              # 移除
#>
[CmdletBinding()]
param(
    [string]$Webhook,
    [string]$ProjectRoot,
    [switch]$Remove,
    [switch]$Show,
    [switch]$NoTest
)

$ErrorActionPreference = 'Stop'

if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
if (-not (Test-Path -LiteralPath $ProjectRoot)) { throw "项目目录不存在: $ProjectRoot" }
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

$secretsPath = Join-Path $ProjectRoot 'config\secrets.local.toml'
$keyName = 'OPS_WECOM_WEBHOOK'
$prefix = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key='

function Read-Secrets {
    if (-not (Test-Path -LiteralPath $secretsPath)) { return @() }
    return @(Get-Content -LiteralPath $secretsPath -Encoding UTF8)
}

function Write-Secrets([string[]]$Lines) {
    # 必须写成「UTF-8 无 BOM」：TOML 解析器遇到 BOM 会报错。
    # PowerShell 5.1 的 Set-Content -Encoding UTF8 会加 BOM，所以走 .NET。
    $dir = Split-Path -Parent $secretsPath
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    $text = ($Lines -join [Environment]::NewLine) + [Environment]::NewLine
    [System.IO.File]::WriteAllText($secretsPath, $text, (New-Object System.Text.UTF8Encoding($false)))
}

function Get-CurrentValue {
    foreach ($line in (Read-Secrets)) {
        if ($line -match "^\s*$keyName\s*=\s*""(.*)""\s*$") { return $Matches[1] }
    }
    return $null
}

function Format-Masked([string]$Value) {
    if (-not $Value) { return '（未配置）' }
    if ($Value.Length -le 20) { return '（已配置，长度过短，疑似不完整）' }
    return $Value.Substring(0, 18) + '…' + $Value.Substring($Value.Length - 4)
}

# 环境变量优先：如果它已存在，会盖住文件里的值，这里必须提醒
function Get-EnvOverride {
    $value = [Environment]::GetEnvironmentVariable($keyName, 'User')
    if (-not $value) { $value = [Environment]::GetEnvironmentVariable($keyName, 'Machine') }
    return $value
}

# ---------------------------------------------------------------- 只查看
if ($Show) {
    $envValue = Get-EnvOverride
    $fileValue = Get-CurrentValue
    Write-Host "配置文件   : $secretsPath"
    Write-Host "文件里的值 : $(Format-Masked $fileValue)"
    if ($envValue) {
        Write-Host "环境变量   : $(Format-Masked $envValue)  ← 优先级更高，会盖住文件里的值" -ForegroundColor Yellow
    } else {
        Write-Host "环境变量   : （未设置）"
    }
    if (-not $fileValue -and -not $envValue) {
        Write-Host ""
        Write-Host "还没配置。运行时企业微信会走 dry-run（消息落本地日志）。" -ForegroundColor Yellow
    }
    return
}

# ---------------------------------------------------------------- 移除
if ($Remove) {
    $lines = Read-Secrets
    $kept = @($lines | Where-Object { $_ -notmatch "^\s*$keyName\s*=" })
    if ($kept.Count -eq $lines.Count) {
        Write-Host "文件里本来就没有 $keyName，未改动。"
    } else {
        Write-Secrets $kept
        Write-Host "已从 $secretsPath 移除 $keyName" -ForegroundColor Green
    }
    if (Get-EnvOverride) {
        Write-Warning "注意：用户/系统环境变量里仍有 $keyName，它仍会生效。需要的话手动清掉。"
    }
    return
}

# ---------------------------------------------------------------- 写入
if (-not $Webhook) {
    Write-Host "请粘贴企业微信群机器人的 Webhook 地址，然后回车："
    Write-Host "（企业微信 → 任意群 → 右上角 … → 群机器人 → 添加机器人 → 复制 Webhook 地址）"
    $Webhook = (Read-Host "Webhook").Trim()
}
if (-not $Webhook) { throw "没有输入内容，未做任何改动。" }
if (-not $Webhook.StartsWith($prefix)) {
    throw "这不像企业微信群机器人的 webhook：应以 $prefix 开头。请重新复制一次。"
}

$existing = Read-Secrets
$kept = @($existing | Where-Object { $_ -notmatch "^\s*$keyName\s*=" })
$kept += "$keyName = ""$Webhook"""
Write-Secrets $kept

Write-Host "已写入 $secretsPath" -ForegroundColor Green
Write-Host "  $keyName = $(Format-Masked $Webhook)"
if (Get-EnvOverride) {
    Write-Warning "环境变量里也有一份 $keyName，它的优先级更高 —— 会盖住刚写入的值。"
}

if (-not $NoTest) {
    Write-Host ""
    Write-Host "发一条测试消息验证通道：" -ForegroundColor Cyan
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) { throw "找不到 python，无法自动测试；请手动运行 tools\notify_test.py" }
    & $python (Join-Path $ProjectRoot 'tools\notify_test.py') --config (Join-Path $ProjectRoot 'config\config.toml')
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Host ""
        Write-Host "测试没通过（退出码 $code）。检查 webhook 是否复制完整、机器人是否还在群里。" -ForegroundColor Yellow
    }
    exit $code
}
