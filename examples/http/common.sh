#!/usr/bin/env bash
# Sourced by the cURL examples. Never enable shell tracing with a credential.
set -euo pipefail
: "${MULTIRAG_API_KEY:?Set MULTIRAG_API_KEY; see examples/README.md}"
command -v curl >/dev/null
command -v jq >/dev/null
base="${MULTIRAG_BASE_URL:-http://127.0.0.1:8123}"
base="${base%/}/api/v1"
timeout=$(jq -en --arg value "${MULTIRAG_TIMEOUT_SECONDS:-30}" '$value | tonumber | select(. > 0 and . <= 300)')
request() {
    local method="$1" path="$2" payload="${3:-}" response
    local args=(--silent --show-error --fail-with-body --connect-timeout 5 --max-time "$timeout"
        --request "$method" --header "Authorization: Bearer ${MULTIRAG_API_KEY}")
    if [[ -n "$payload" ]]; then
        args+=(--header 'Content-Type: application/json' --data "$payload")
    fi
    response=$(curl "${args[@]}" "$base$path") || return $?
    if ! jq -e '.code == 0' >/dev/null <<<"$response"; then
        echo "$method $path: business failure" >&2
        return 1
    fi
    jq '.data' <<<"$response"
}
