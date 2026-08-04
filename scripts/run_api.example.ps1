# Launch the MultiRAG API with its per-process Channel secrets (Windows).
#
# Reads the env file produced by init_channel_secrets.example.ps1. Values
# already present in the environment win, so CI and production injection are
# unaffected. Nothing is echoed.
#
# Copy to run_api.local.ps1 if you need to customise it.

[CmdletBinding()]
param(
    # Set when this deployment genuinely has no managed Channel bindings.
    [switch]$AllowMissingChannelSecrets
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$secretsDir = $env:MULTIRAG_SECRETS_DIR
if ([string]::IsNullOrWhiteSpace($secretsDir)) {
    $secretsDir = Join-Path $env:LOCALAPPDATA "MultiRAG\secrets"
}
$apiEnvPath = Join-Path $secretsDir "api.env"

if (Test-Path $apiEnvPath) {
    foreach ($line in [System.IO.File]::ReadAllLines($apiEnvPath)) {
        $trimmed = $line.Trim()
        if ($trimmed -eq "" -or $trimmed.StartsWith("#")) { continue }
        $separator = $trimmed.IndexOf("=")
        if ($separator -lt 1) { continue }
        $name = $trimmed.Substring(0, $separator).Trim()
        $value = $trimmed.Substring($separator + 1)
        # Pre-set values win so an operator can override the file.
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, "Process"))) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

# Without the key the control plane fails closed at request time instead of at
# startup, which reads as "the bot broke" rather than "the key is missing".
# Refuse early and say so.
if (-not $AllowMissingChannelSecrets) {
    if ([string]::IsNullOrWhiteSpace($env:MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY)) {
        throw "MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY is not set (looked in $apiEnvPath). Run scripts/init_channel_secrets.example.ps1 once per machine, or pass -AllowMissingChannelSecrets to start without managed Channels."
    }
    if ([string]::IsNullOrWhiteSpace($env:MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN)) {
        throw "MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN is not set (looked in $apiEnvPath); the private Channel runtime API would stay disabled."
    }
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repositoryRoot
try {
    uv run python -m api.multirag_server
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
