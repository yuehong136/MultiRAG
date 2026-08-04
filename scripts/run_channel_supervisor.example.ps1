# Cross-platform MultiRAG Channel supervisor launcher for PowerShell.
#
# Inject the required values into the process environment before invoking this
# script. It intentionally does not contain or persist any secret.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Load the supervisor's own env file when the environment is not already
# populated. It deliberately holds no encryption key, which is why the
# supervisor gets a separate file from the API.
$secretsDir = $env:MULTIRAG_SECRETS_DIR
if ([string]::IsNullOrWhiteSpace($secretsDir)) {
    $secretsDir = Join-Path $env:LOCALAPPDATA "MultiRAG\secrets"
}
$supervisorEnvPath = Join-Path $secretsDir "supervisor.env"
if (Test-Path $supervisorEnvPath) {
    foreach ($line in [System.IO.File]::ReadAllLines($supervisorEnvPath)) {
        $trimmed = $line.Trim()
        if ($trimmed -eq "" -or $trimmed.StartsWith("#")) { continue }
        $separator = $trimmed.IndexOf("=")
        if ($separator -lt 1) { continue }
        $name = $trimmed.Substring(0, $separator).Trim()
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, "Process"))) {
            [Environment]::SetEnvironmentVariable($name, $trimmed.Substring($separator + 1), "Process")
        }
    }
}

$requiredVariables = @(
    "MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL",
    "MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN"
)

foreach ($variableName in $requiredVariables) {
    $value = [Environment]::GetEnvironmentVariable($variableName, "Process")
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Inject $variableName into the process environment before starting the supervisor."
    }
}

if (-not [string]::IsNullOrWhiteSpace($env:MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY)) {
    throw "Do not grant the Channel supervisor the control-plane secret encryption key."
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repositoryRoot
try {
    uv run python -m api.channels.supervisor
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
