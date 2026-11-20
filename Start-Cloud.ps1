[CmdletBinding()]
param([string]$EnvFile = (Join-Path $PSScriptRoot 'env/.env'))
$ErrorActionPreference = 'Stop'
$EnvFile = [IO.Path]::GetFullPath($EnvFile)
. (Join-Path $PSScriptRoot 'deploy/config.ps1')
$config = Read-ViConfig $EnvFile
$ssh = Get-ViSsh $config
$webPort = Get-ViPort $config 'WEB_PORT' 3166
$remotePort = Get-ViPort $config 'CLOUD_WEB_PORT' 3166
$listener = Get-NetTCPConnection -LocalPort $webPort -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $processes = $listener | ForEach-Object { Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue }
    if ($processes | Where-Object { $_.ProcessName -ne 'ssh' }) { throw 'Web port is in use. Stop the local web service before starting the cloud tunnel.' }
} else {
    $arguments = @('-N','-L',('127.0.0.1:'+$webPort+':127.0.0.1:'+$remotePort),
        '-i',('"'+$ssh.Identity+'"'),'-o','BatchMode=yes','-o','IdentitiesOnly=yes',
        '-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
        '-o','StrictHostKeyChecking=yes','-o',('"UserKnownHostsFile='+$ssh.KnownHosts+'"'),$ssh.Target)
    Start-Process -FilePath 'ssh.exe' -ArgumentList $arguments -WindowStyle Hidden
}
Write-Host ('Cloud address: http://localhost:'+$webPort)
