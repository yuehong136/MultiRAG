#!/bin/bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

# Match CI's package discovery and race checks, including new internal packages.
# Additional go test flags, such as -run or -json, support focused local checks.
exec go test -race -count=1 -v -cover "$@" ./internal/...
