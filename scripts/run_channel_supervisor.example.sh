#!/usr/bin/env sh
# Portable MultiRAG Channel supervisor launcher for macOS and Linux.
# Inject secrets into the environment before invoking this script. It never
# writes them to disk or passes them as command-line arguments.

set -eu

: "${MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL:?Inject the runtime API base URL before starting the supervisor}"
: "${MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN:?Inject the internal API token before starting the supervisor}"

if [ -n "${MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY:-}" ]; then
    echo "Do not grant the Channel supervisor the control-plane secret encryption key." >&2
    exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$repository_root"

exec uv run python -m api.channels.supervisor
