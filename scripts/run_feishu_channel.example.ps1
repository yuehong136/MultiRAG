# MultiRAG Feishu Channel local demo launcher.
#
# Copy this file to run_feishu_channel.local.ps1, replace the four required
# placeholders, and keep that local file out of source control. The local file
# is ignored by this repository but still contains plaintext secrets on disk.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Required: replace these four values.
$env:MULTIRAG_CHANNELS__FEISHU__APP_ID = "REPLACE_WITH_FEISHU_APP_ID"
$env:MULTIRAG_CHANNELS__FEISHU__APP_SECRET = "REPLACE_WITH_FEISHU_APP_SECRET"
$env:MULTIRAG_CHANNELS__FEISHU__AGENT_ID = "REPLACE_WITH_MULTIRAG_AGENT_ID"
$env:MULTIRAG_CHANNELS__FEISHU__AGENT_API_TOKEN = "REPLACE_WITH_MULTIRAG_STANDARD_API_TOKEN"

# Ready-to-use local demo defaults.
$env:MULTIRAG_CHANNELS__FEISHU__ENABLED = "true"
$env:MULTIRAG_CHANNELS__FEISHU__MULTIRAG_BASE_URL = "http://127.0.0.1:8123"
$env:MULTIRAG_CHANNELS__FEISHU__RELEASE_MARKER = "leadership-demo-v1"
$env:MULTIRAG_CHANNELS__FEISHU__DOMAIN = "feishu"

# First demo: rely on the Feishu application availability scope. Limit that
# scope to your own account before leaving this allowlist empty.
$env:MULTIRAG_CHANNELS__FEISHU__ALLOWED_OPEN_IDS = "[]"

$requiredVariables = @(
    "MULTIRAG_CHANNELS__FEISHU__APP_ID",
    "MULTIRAG_CHANNELS__FEISHU__APP_SECRET",
    "MULTIRAG_CHANNELS__FEISHU__AGENT_ID",
    "MULTIRAG_CHANNELS__FEISHU__AGENT_API_TOKEN"
)

foreach ($variableName in $requiredVariables) {
    $value = [Environment]::GetEnvironmentVariable($variableName, "Process")
    if ([string]::IsNullOrWhiteSpace($value) -or $value.StartsWith("REPLACE_WITH_")) {
        throw "Replace the placeholder for $variableName before starting the channel."
    }
}

if (-not $env:MULTIRAG_CHANNELS__FEISHU__APP_ID.StartsWith("cli_")) {
    throw "The Feishu App ID should normally start with cli_."
}
if (-not $env:MULTIRAG_CHANNELS__FEISHU__AGENT_API_TOKEN.StartsWith("multirag-")) {
    throw "Use data.token from POST /api/v1/system/tokens, not the beta token."
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repositoryRoot
try {
    uv run python -m api.channels.worker --channel feishu
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
