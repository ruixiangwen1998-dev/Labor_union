$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptRoot
$envFile = Join-Path $root ".env"

$desired = [ordered]@{
    APP_ENV = "development"
    ENABLE_ADMIN_AUTH = "false"
    INTERNAL_API_KEY = ""
}

$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes) | Out-Null
$desired["INTERNAL_API_KEY"] = [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_").Replace("=", "")

$existing = @()
if (Test-Path $envFile) {
    $existing = Get-Content -Path $envFile
}

$seen = @{}
$next = @()
foreach ($line in $existing) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
        $key = $matches[1]
        if ($desired.Contains($key)) {
            if (-not $seen.ContainsKey($key)) {
                $next += "$($key)=$($desired[$key])"
                $seen[$key] = $true
            }
            continue
        }
    }
    $next += $line
}

foreach ($key in $desired.Keys) {
    if (-not $seen.ContainsKey($key)) {
        $next += "$($key)=$($desired[$key])"
    }
}

Set-Content -Path $envFile -Value $next -Encoding UTF8

Write-Host "[OK] .env 已更新："
Write-Host "APP_ENV=$($desired['APP_ENV'])"
Write-Host "ENABLE_ADMIN_AUTH=$($desired['ENABLE_ADMIN_AUTH'])"
Write-Host "INTERNAL_API_KEY=已更新（值不顯示）"
