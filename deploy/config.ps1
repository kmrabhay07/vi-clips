function Read-ViConfig([string]$Path) {
    if (!(Test-Path -LiteralPath $Path)) { throw 'Copy env/.env.example to env/.env and configure it first.' }
    $values = @{}
    foreach ($line in Get-Content -Encoding UTF8 -LiteralPath $Path) {
        $text = $line.Trim()
        if (!$text -or $text.StartsWith('#')) { continue }
        $parts = $text -split '=', 2
        if ($parts.Count -ne 2) { throw 'Invalid environment file entry.' }
        $value = $parts[1].Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1,$value.Length-2)
        }
        $values[$parts[0].Trim()] = $value
    }
    return $values
}

function Get-ViValue($Config,[string]$Key,[string]$Default='') {
    if ($Config.ContainsKey($Key) -and $Config[$Key]) { return $Config[$Key] }
    return $Default
}

function Get-ViPort($Config,[string]$Key,[int]$Default) {
    $port = [int](Get-ViValue $Config $Key ([string]$Default))
    if ($port -lt 1 -or $port -gt 65535) { throw "Invalid port setting: $Key" }
    return $port
}

function Get-ViSsh($Config) {
    $sshHost = Get-ViValue $Config 'SSH_HOST'
    $sshUser = Get-ViValue $Config 'SSH_USER'
    $identity = Get-ViValue $Config 'SSH_IDENTITY_FILE'
    $knownHosts = Get-ViValue $Config 'SSH_KNOWN_HOSTS_FILE'
    if ($sshHost -notmatch '^[A-Za-z0-9][A-Za-z0-9.:-]*$' -or $sshUser -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]*$') {
        throw 'Set SSH_HOST and SSH_USER in the private environment file.'
    }
    if (!(Test-Path -LiteralPath $identity -PathType Leaf) -or !(Test-Path -LiteralPath $knownHosts -PathType Leaf)) {
        throw 'Set valid SSH_IDENTITY_FILE and SSH_KNOWN_HOSTS_FILE paths in the private environment file.'
    }
    return @{Target=($sshUser+'@'+$sshHost);Identity=$identity;KnownHosts=$knownHosts}
}
