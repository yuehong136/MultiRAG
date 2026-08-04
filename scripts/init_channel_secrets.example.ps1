# Create the per-process Channel secret files for one machine (Windows).
#
# The Channel master encryption key must NOT live in configs/*.yaml: every
# process that calls get_app_config() reads configs/local.service_conf.yaml,
# including the worker children the supervisor forks, which would defeat the
# env scrubbing in api/channels/supervisor.py::_spawn_worker. So the key is
# written to a per-process env file outside the repository and injected only
# into the API process.
#
# Values are generated in-process and never printed. Only the non-secret
# key fingerprint is echoed so you can later confirm which key encrypted a
# stored credential (it equals ChannelSecret.key_id in the database).
#
# Copy to init_channel_secrets.local.ps1 if you need to customise it.

[CmdletBinding()]
param(
    # Rotating the key makes every stored provider credential undecryptable:
    # there is no re-encryption flow yet. Refuse unless explicitly forced.
    [switch]$Force,
    [string]$RuntimeApiBaseUrl = "http://127.0.0.1:8123"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function New-UrlSafeBase64 {
    param([int]$ByteCount)

    $buffer = [byte[]]::new($ByteCount)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($buffer)
    }
    finally {
        $rng.Dispose()
    }
    $encoded = [Convert]::ToBase64String($buffer).Replace('+', '-').Replace('/', '_').TrimEnd('=')
    return [pscustomobject]@{ Encoded = $encoded; Bytes = $buffer }
}

function Get-KeyFingerprint {
    param([byte[]]$KeyBytes)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash($KeyBytes)
    }
    finally {
        $sha.Dispose()
    }
    return -join ($digest[0..7] | ForEach-Object { $_.ToString('x2') })
}

$secretsDir = $env:MULTIRAG_SECRETS_DIR
if ([string]::IsNullOrWhiteSpace($secretsDir)) {
    $secretsDir = Join-Path $env:LOCALAPPDATA "MultiRAG\secrets"
}
$apiEnvPath = Join-Path $secretsDir "api.env"
$supervisorEnvPath = Join-Path $secretsDir "supervisor.env"

if ((Test-Path $apiEnvPath) -and -not $Force) {
    throw "$apiEnvPath already exists. Reusing the existing key keeps stored credentials decryptable; pass -Force only when you accept re-entering every provider secret."
}

if (-not (Test-Path $secretsDir)) {
    New-Item -ItemType Directory -Path $secretsDir -Force | Out-Null
}

# Strip inheritance so only the current user (and SYSTEM) can read the files.
& icacls.exe $secretsDir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null

$key = New-UrlSafeBase64 -ByteCount 32
$token = New-UrlSafeBase64 -ByteCount 48
$fingerprint = Get-KeyFingerprint -KeyBytes $key.Bytes

# API owns the master encryption key; the supervisor deliberately does not.
$apiLines = @(
    "MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY=$($key.Encoded)",
    "MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN=$($token.Encoded)"
)
$supervisorLines = @(
    "MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL=$RuntimeApiBaseUrl",
    "MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN=$($token.Encoded)"
)

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($apiEnvPath, ($apiLines -join "`n") + "`n", $utf8NoBom)
[System.IO.File]::WriteAllText($supervisorEnvPath, ($supervisorLines -join "`n") + "`n", $utf8NoBom)

Write-Output "Wrote $apiEnvPath"
Write-Output "Wrote $supervisorEnvPath"
Write-Output "Channel key fingerprint (non-secret, equals ChannelSecret.key_id): $fingerprint"
Write-Output "Back these files up in your password manager: a lost key cannot be recovered and stored provider credentials become undecryptable."
