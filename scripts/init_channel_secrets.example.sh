#!/usr/bin/env sh
# Create the per-process Channel secret files for one machine (macOS / Linux).
#
# The Channel master encryption key must NOT live in configs/*.yaml: every
# process that calls get_app_config() reads configs/local.service_conf.yaml,
# including the worker children the supervisor forks, which would defeat the
# env scrubbing in api/channels/supervisor.py::_spawn_worker. So the key is
# written to a per-process env file outside the repository and injected only
# into the API process.
#
# Values are generated in-process and never printed. Only the non-secret key
# fingerprint is echoed; it equals ChannelSecret.key_id in the database.
#
# Usage:  sh scripts/init_channel_secrets.example.sh [--force] [runtime_api_base_url]

set -eu

force=0
runtime_api_base_url="http://127.0.0.1:8123"
for argument in "$@"; do
    case "$argument" in
        --force) force=1 ;;
        *) runtime_api_base_url="$argument" ;;
    esac
done

secrets_dir="${MULTIRAG_SECRETS_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/multirag/secrets}"
api_env="$secrets_dir/api.env"
supervisor_env="$secrets_dir/supervisor.env"

if [ -f "$api_env" ] && [ "$force" -eq 0 ]; then
    echo "$api_env already exists. Reusing the existing key keeps stored credentials decryptable;" >&2
    echo "pass --force only when you accept re-entering every provider secret." >&2
    exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 is required to generate the secrets" >&2; exit 1; }

mkdir -p "$secrets_dir"
chmod 700 "$secrets_dir"

# One python invocation writes both files, so no secret ever reaches stdout,
# the shell's argument list, or the shell history.
MULTIRAG_API_ENV="$api_env" \
MULTIRAG_SUPERVISOR_ENV="$supervisor_env" \
MULTIRAG_RUNTIME_API_BASE_URL="$runtime_api_base_url" \
python3 - <<'PY'
import base64
import hashlib
import os
import secrets


def url_safe(byte_count: int) -> tuple[str, bytes]:
    raw = secrets.token_bytes(byte_count)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii"), raw


key, key_bytes = url_safe(32)
token, _ = url_safe(48)

api_path = os.environ["MULTIRAG_API_ENV"]
supervisor_path = os.environ["MULTIRAG_SUPERVISOR_ENV"]
base_url = os.environ["MULTIRAG_RUNTIME_API_BASE_URL"]


def write(path: str, lines: list[str]) -> None:
    # Create with 0600 from the start so the secret is never briefly readable.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# API owns the master encryption key; the supervisor deliberately does not.
write(api_path, [
    f"MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY={key}",
    f"MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN={token}",
])
write(supervisor_path, [
    f"MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL={base_url}",
    f"MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN={token}",
])

print(f"Wrote {api_path}")
print(f"Wrote {supervisor_path}")
print(f"Channel key fingerprint (non-secret, equals ChannelSecret.key_id): {hashlib.sha256(key_bytes).hexdigest()[:16]}")
PY

echo "Back these files up in your password manager: a lost key cannot be recovered"
echo "and stored provider credentials become undecryptable."
