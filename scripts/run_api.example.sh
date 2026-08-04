#!/usr/bin/env sh
# Launch the MultiRAG API with its per-process Channel secrets (macOS / Linux).
#
# Reads the env file produced by init_channel_secrets.example.sh. Values already
# present in the environment win, so CI and production injection are unaffected.
# Nothing is echoed.
#
# Usage:  sh scripts/run_api.example.sh [--allow-missing-channel-secrets]

set -eu

allow_missing=0
for argument in "$@"; do
    case "$argument" in
        --allow-missing-channel-secrets) allow_missing=1 ;;
    esac
done

secrets_dir="${MULTIRAG_SECRETS_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/multirag/secrets}"
api_env="$secrets_dir/api.env"

if [ -f "$api_env" ]; then
    # Pre-set values win so an operator can override the file.
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|'#'*) continue ;;
        esac
        name=${line%%=*}
        [ "$name" = "$line" ] && continue
        eval "current=\${$name:-}"
        if [ -z "$current" ]; then
            export "$name=${line#*=}"
        fi
    done < "$api_env"
fi

# Without the key the control plane fails closed at request time instead of at
# startup, which reads as "the bot broke" rather than "the key is missing".
if [ "$allow_missing" -eq 0 ]; then
    if [ -z "${MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY:-}" ]; then
        echo "MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY is not set (looked in $api_env)." >&2
        echo "Run scripts/init_channel_secrets.example.sh once per machine, or pass --allow-missing-channel-secrets." >&2
        exit 1
    fi
    if [ -z "${MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN:-}" ]; then
        echo "MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN is not set (looked in $api_env);" >&2
        echo "the private Channel runtime API would stay disabled." >&2
        exit 1
    fi
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$repository_root"

exec uv run python -m api.multirag_server
