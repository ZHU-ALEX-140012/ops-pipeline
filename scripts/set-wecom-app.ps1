<#
.SYNOPSIS
    配置企业微信「自建应用」告警通道（写入本地密钥文件）。

.DESCRIPTION
    为什么用自建应用而不是群机器人：**群机器人需要普通内部群**，
    在「全员群」和单人企业里连"添加群机器人"的入口都不会出现。
    自建应用没有这个限制，一个人也能收到推送。

    需要三个值，都在企业微信管理后台（work.weixin.qq.com）：
        CorpID   我的企业 → 企业信息 → 最下方「企业ID」
        AgentId  应用管理 → 自建 → 创建应用 → 建好后在应用详情页
        Secret   同上，点「查看」（会发到企业微信里给你看）
    ⚠️ 建好应用后，务必把它的**可见范围**设成包含你自己，否则推送会返回 invaliduser。

    三个值写进 config\secrets.local.toml（已在 .gitignore 中）。

.EXAMPLE
    .\set-wecom-app.ps1            # 交互式录入三个值，然后立刻发一条测试消息
.EXAMPLE
    .\set-wecom-app.ps1 -Show      # 看当前配置状态（值打码）
.EXAMPLE
    .\set-wecom-app.ps1 -Remove    # 移除这三个值
#>
[CmdletBinding()]
param(
    [string]$CorpId,
    [string]$Secret,
    [string]$AgentId,
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
$keys = [ordered]@{
    OPS_WECOM_CORPID  = 'CorpID'
    OPS_WECOM_SECRET  = 'Secret'
    OPS_WECOM_AGENTID = 'AgentId'
}

function Read-Secrets {
    if (-not (Test-Path -LiteralPath $secretsPath)) { return @() }
    return @(Get-Content -LiteralPath $secretsPath -Encoding UTF8)
}

function Write-Secrets([string[]]$Lines) {
    # 必须写成「UTF-8 无 BOM」：TOML 解析器遇到 BOM 会报错。
    $dir = Split-Path -Parent $secretsPath
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    $text = ($Lines -join [Environment]::NewLine) + [Environment]::NewLine
    [System.IO.File]::WriteAllText($secretsPath, $text, (New-Object System.Text.UTF8Encoding($false)))
}

function Get-ValueOf([string]$Name) {
    foreach ($line in (Read-Secrets)) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*""(.*)""\s*$") { return $Matches[1] }
    }
    return $null
}

function Format-Masked([string]$Value) {
    if (-not $Value) { return '（未配置）' }
    if ($Value.Length -le 12) { return '（已配置）' }
    return $Value.Substring(0, 6) + '…' + $Value.Substring($Value.Length - 4)
}

# ---------------------------------------------------------------- 只查看
if ($Show) {
    Write-Host "密钥文件 : $secretsPath"
    foreach ($name in $keys.Keys) {
        $fromEnv = [Environment]::GetEnvironmentVariable($name, 'User')
        if (-not $fromEnv) { $fromEnv = [Environment]::GetEnvironmentVariable($name, 'Machine') }
        $fromFile = Get-ValueOf $name
        $state = if ($fromEnv) { "$(Format-Masked $fromEnv)  ← 来自环境变量（优先级更高）" }
                 elseif ($fromFile) { Format-Masked $fromFile }
                 else { '（未配置）' }
        Write-Host ("  {0,-18} {1}  {2}" -f $keys[$name], $name, $state)
    }
    $all = @($keys.Keys | Where-Object { (Get-ValueOf $_) }).Count
    if ($all -lt $keys.Count) {
        Write-Host ""
        Write-Host "还缺 $($keys.Count - $all) 个值。补齐后运行本脚本或 tools\notify_test.py --channel wecom_app 验证。" -ForegroundColor Yellow
    }
    return
}

# ---------------------------------------------------------------- 移除
if ($Remove) {
    $lines = Read-Secrets
    $kept = @($lines | Where-Object {
        $line = $_
        -not ($keys.Keys | Where-Object { $line -match "^\s*$([regex]::Escape($_))\s*=" })
    })
    if ($kept.Count -eq $lines.Count) {
        Write-Host "文件里本来就没有这些键，未改动。"
    } else {
        Write-Secrets $kept
        Write-Host "已从 $secretsPath 移除企业微信自建应用的三个键" -ForegroundColor Green
    }
    return
}

# ---------------------------------------------------------------- 录入
# 已有的值会显示出来（打码）并支持"直接回车沿用" —— 这样补一个值不用把三个都重敲，
# 也避免手滑覆盖掉已经配好的项。
function Read-WithDefault([string]$Label, [string]$Hint, [string]$Current) {
    Write-Host ""
    if ($Hint) { Write-Host $Hint }
    if ($Current) {
        Write-Host ("  当前已配置：{0}" -f (Format-Masked $Current)) -ForegroundColor DarkGray
        $entered = (Read-Host "$Label（回车沿用）").Trim()
        if (-not $entered) { return $Current }
        return $entered
    }
    return (Read-Host $Label).Trim()
}

if (-not $CorpId) {
    $CorpId = Read-WithDefault "CorpID" `
        "管理后台 → 我的企业 → 团队信息 → 最下方「团队ID」（新版后台把「企业ID」改叫「团队ID」了）" `
        (Get-ValueOf 'OPS_WECOM_CORPID')
}
if (-not $AgentId) {
    $AgentId = Read-WithDefault "AgentId" `
        "管理后台 → 应用管理 → 应用 → 自建 → 你的应用 → 详情页上的 AgentId（纯数字）" `
        (Get-ValueOf 'OPS_WECOM_AGENTID')
}
if (-not $Secret) {
    $Secret = Read-WithDefault "Secret" `
        "同一个应用详情页 → Secret → 查看（会发一条消息到企业微信里，点开就能看到）" `
        (Get-ValueOf 'OPS_WECOM_SECRET')
}

if (-not $CorpId) { throw "CorpID 不能为空，未做任何改动。" }
if (-not $AgentId) { throw "AgentId 不能为空，未做任何改动。" }
if (-not $Secret) { throw "Secret 不能为空，未做任何改动。" }
if ($AgentId -notmatch '^\d+$') { throw "AgentId 应该是纯数字，收到：$AgentId" }
if ($CorpId.Length -lt 6) { Write-Warning "CorpID 看起来偏短，确认一下是不是复制完整。" }

$values = [ordered]@{
    OPS_WECOM_CORPID  = $CorpId
    OPS_WECOM_SECRET  = $Secret
    OPS_WECOM_AGENTID = $AgentId
}

$existing = Read-Secrets
$kept = @($existing | Where-Object {
    $line = $_
    -not ($values.Keys | Where-Object { $line -match "^\s*$([regex]::Escape($_))\s*=" })
})
foreach ($name in $values.Keys) { $kept += "$name = ""$($values[$name])""" }
Write-Secrets $kept

Write-Host ""
Write-Host "已写入 $secretsPath" -ForegroundColor Green
foreach ($name in $keys.Keys) {
    Write-Host ("  {0,-18} {1}" -f $keys[$name], (Format-Masked (Get-ValueOf $name)))
}

if (-not $NoTest) {
    Write-Host ""
    Write-Host "发一条测试消息验证通道：" -ForegroundColor Cyan
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) { throw "找不到 python，无法自动测试；请手动运行 tools\notify_test.py --channel wecom_app" }
    & $python (Join-Path $ProjectRoot 'tools\notify_test.py') --channel wecom_app --config (Join-Path $ProjectRoot 'config\config.toml')
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Host ""
        Write-Host "测试没通过（退出码 $code）。常见原因：" -ForegroundColor Yellow
        Write-Host "  1) CorpID / Secret 抄错或多了空格"
        Write-Host "  2) 应用的可见范围没包含你自己（会报 invaliduser）"
        Write-Host "  3) 应用没启用或 AgentId 不对"
    }
    exit $code
}
