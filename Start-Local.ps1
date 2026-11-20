[CmdletBinding()]
param([string]$EnvFile = (Join-Path $PSScriptRoot 'env/.env'))
$ErrorActionPreference = 'Stop'
$EnvFile = [IO.Path]::GetFullPath($EnvFile)
. (Join-Path $PSScriptRoot 'deploy/config.ps1')
if (!(Test-Path -LiteralPath $EnvFile)) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $EnvFile) -Force | Out-Null
    $bytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    $password = [BitConverter]::ToString($bytes).Replace('-','').ToLowerInvariant()
    $template = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'env/.env.example')
    $template = $template -replace '(?m)^POSTGRES_PASSWORD=.*$', ('POSTGRES_PASSWORD='+$password)
    [IO.File]::WriteAllText($EnvFile,$template,(New-Object Text.UTF8Encoding($false)))
    Write-Host 'Created private environment configuration with a generated database password.'
}
$config = Read-ViConfig $EnvFile
if (!(Get-ViValue $config 'POSTGRES_PASSWORD')) { throw 'Set POSTGRES_PASSWORD in the private environment file.' }
$port = Get-ViPort $config 'WEB_PORT' 3166
Push-Location $PSScriptRoot
try {
    docker compose --env-file $EnvFile up --build -d
    if ($LASTEXITCODE -ne 0) { throw 'Local startup failed. Check Docker Desktop and Compose logs.' }
} finally { Pop-Location }
Write-Host ('VI Clips local app: http://localhost:'+$port)
