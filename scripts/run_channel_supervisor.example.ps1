# Cross-platform MultiRAG Channel supervisor launcher for PowerShell.
#
# Inject the required values into the process environment before invoking this
# script. It intentionally does not contain or persist any secret.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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
