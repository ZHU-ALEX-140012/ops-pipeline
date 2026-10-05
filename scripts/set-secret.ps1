<#
.SYNOPSIS
    通用凭据录入工具：把任意一个凭据写进 config\secrets.local.toml。

.DESCRIPTION
    为什么做成通用的：告警通道会换（企业微信自建应用 / 群机器人 / ntfy / 邮件），
    每个都写一个专用脚本会越来越多。这里只管"一个键 + 一个值"，
    剩下的交给 config.toml 里的 *_env 去引用。

    值只写进本地密钥文件（已在 .gitignore 中），不进版本库、不进对话记录。

.EXAMPLE
    .\set-secret.ps1 -Name OPS_NTFY_URL -Hint "https://ntfy.sh/你的随机话题"
.EXAMPLE
    .\set-secret.ps1 -List                 # 看已配置了哪些（值打码）
.EXAMPLE
    .\set-secret.ps1 -Name OPS_MAIL_PASS -Remove
#>
[CmdletBinding()]
param(
    [string]$Name,
    [string]$Value,
    [string]$Hint,
    [string]$ProjectRoot,
    [switch]$Masked,
    [switch]$Remove,
    [switch]$List
)

$ErrorActionPreference = 'Stop'

if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
if (-not (Test-Path -LiteralPath $ProjectRoot)) { throw "项目目录不存在: $ProjectRoot" }
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$secretsPath = Join-Path $ProjectRoot 'config\secrets.local.toml'

function Read-Secrets {
    if (-not (Test-Path -LiteralPath $secretsPath)) { return @() }
    return @(Get-Content -LiteralPath $secretsPath -Encoding UTF8)
}

function Write-Secrets([string[]]$Lines) {
    # UTF-8 无 BOM：TOML 解析器遇到 BOM 会报错
    $text = ($Lines -join [Environment]::NewLine) + [Environment]::NewLine
    [System.IO.File]::WriteAllText($secretsPath, $text, (New-Object System.Text.UTF8Encoding($false)))
}

function Format-Masked([string]$v) {
    if (-not $v) { return '（空）' }
    if ($v.Length -le 14) { return '（已配置，长度 ' + $v.Length + '）' }
    return $v.Substring(0, 8) + '…' + $v.Substring($v.Length - 4)
}

# ---------------------------------------------------------------- 列出
if ($List) {
    $lines = @(Read-Secrets | Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*\s*=' })
    Write-Host "密钥文件 : $secretsPath"
    if ($lines.Count -eq 0) {
        Write-Host "  （还没配置任何凭据）" -ForegroundColor Yellow
    } else {
        foreach ($line in $lines) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"(.*)"\s*$') {
                Write-Host ("  {0,-22} {1}" -f $Matches[1], (Format-Masked $Matches[2]))
            }
        }
    }
    return
}

if (-not $Name) { throw "必须用 -Name 指定键名（例如 OPS_NTFY_URL）" }
if ($Name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw "键名只能是字母/数字/下划线，收到：$Name" }

# ---------------------------------------------------------------- 删除
if ($Remove) {
    $lines = Read-Secrets
    $kept = @($lines | Where-Object { $_ -notmatch "^\s*$([regex]::Escape($Name))\s*=" })
    if ($kept.Count -eq $lines.Count) {
        Write-Host "文件里没有 $Name，未改动。"
    } else {
        Write-Secrets @($kept | Where-Object { $_.Trim() -ne '' })
        Write-Host "已移除 $Name" -ForegroundColor Green
    }
    return
}

# ---------------------------------------------------------------- 写入
if (-not $Value) {
    if ($Hint) { Write-Host $Hint }
    if ($Masked) {
        $secure = Read-Host "$Name（输入时不显示）" -AsSecureString
        $Value = [System.Net.NetworkCredential]::new('', $secure).Password
    } else {
        $Value = (Read-Host $Name).Trim()
    }
}
if (-not $Value) { throw "值为空，未做任何改动。" }

$existing = Read-Secrets
$kept = @($existing | Where-Object { $_.Trim() -ne '' -and $_ -notmatch "^\s*$([regex]::Escape($Name))\s*=" })
$kept += "$Name = ""$Value"""
Write-Secrets $kept

Write-Host ""
Write-Host "已写入 $secretsPath" -ForegroundColor Green
Write-Host ("  {0,-22} {1}" -f $Name, (Format-Masked $Value))
