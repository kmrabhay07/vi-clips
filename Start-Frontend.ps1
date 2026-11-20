[CmdletBinding()]
param([string]$EnvFile = (Join-Path $PSScriptRoot 'env/.env'))
$ErrorActionPreference = 'Stop'
$EnvFile = [IO.Path]::GetFullPath($EnvFile)
. (Join-Path $PSScriptRoot 'deploy/config.ps1')
$config = Read-ViConfig $EnvFile
$ssh = Get-ViSsh $config
$tunnelPort = Get-ViPort $config 'LOCAL_TUNNEL_PORT' 4166
$remotePort = Get-ViPort $config 'CLOUD_WEB_PORT' 3166
$webPort = Get-ViPort $config 'WEB_PORT' 3166
if (!(Get-NetTCPConnection -LocalPort $tunnelPort -State Listen -ErrorAction SilentlyContinue)) {
    $arguments = @('-N','-L',('127.0.0.1:'+ $tunnelPort + ':127.0.0.1:' + $remotePort),
        '-i',('"'+$ssh.Identity+'"'),'-o','BatchMode=yes','-o','IdentitiesOnly=yes',
        '-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
        '-o','StrictHostKeyChecking=yes','-o',('"UserKnownHostsFile='+$ssh.KnownHosts+'"'),$ssh.Target)
    Start-Process -FilePath 'ssh.exe' -ArgumentList $arguments -WindowStyle Hidden
}
Push-Location $PSScriptRoot
try {
    docker compose --env-file $EnvFile -f docker-compose.yml -f compose.cloud-backend.yml up --no-build --no-deps -d web
    if ($LASTEXITCODE -ne 0) { throw 'Frontend startup failed.' }
} finally { Pop-Location }
Write-Host ('Local frontend with cloud backend: http://localhost:'+$webPort)
