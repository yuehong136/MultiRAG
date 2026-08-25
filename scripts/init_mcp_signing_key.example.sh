#!/usr/bin/env sh
# Generate one unencrypted ES256 / P-256 signing keypair for MultiRAG.
#
# This is key material for identity.mcp_issuer, not a TLS certificate. It does
# not create a CSR and does not contact a CA. The private key stays with the
# MultiRAG issuer; consumers obtain only public keys from the JWKS endpoint.
#
# Usage:
#   sh scripts/init_mcp_signing_key.example.sh \
#     --kid p3-test-2026-08 \
#     [--key-dir /absolute/path/to/secrets/p3] \
#     [--force]

set -eu

usage() {
    cat <<'EOF'
Usage: init_mcp_signing_key.example.sh --kid KID [--key-dir ABSOLUTE_PATH] [--force]

  --kid       1..64 ASCII letters, digits, '_' or '-'; also used in filenames.
  --key-dir   Absolute output directory. Default:
              $MULTIRAG_P3_KEY_DIR, or
              $XDG_CONFIG_HOME/multirag/secrets/p3, or
              $HOME/.config/multirag/secrets/p3.
  --force     Atomically replace only the two files for this exact kid.

Create a new kid for rotation instead of using --force on an active key.
EOF
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

kid=""
force=0
key_dir="${MULTIRAG_P3_KEY_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/multirag/secrets/p3}"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --kid)
            [ "$#" -ge 2 ] || fail "--kid requires a value"
            kid="$2"
            shift 2
            ;;
        --key-dir)
            [ "$#" -ge 2 ] || fail "--key-dir requires a value"
            key_dir="$2"
            shift 2
            ;;
        --force)
            force=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

[ -n "$kid" ] || { usage >&2; fail "--kid is required"; }
case "$kid" in
    *[!A-Za-z0-9_-]*) fail "kid may contain only ASCII letters, digits, '_' or '-'" ;;
esac
[ "${#kid}" -le 64 ] || fail "kid must be at most 64 characters"

case "$key_dir" in
    /*) ;;
    *) fail "--key-dir must be an absolute path" ;;
esac

command -v openssl >/dev/null 2>&1 || fail "openssl is required"
command -v mktemp >/dev/null 2>&1 || fail "mktemp is required"

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
repo_root="$(dirname -- "$script_dir")"
case "$key_dir" in
    "$repo_root"|"$repo_root"/*) fail "key directory must be outside the MultiRAG repository" ;;
esac
case "$key_dir" in
    /|/etc|/opt|/tmp|/usr|/var|"$HOME"|"${XDG_CONFIG_HOME:-$HOME/.config}")
        fail "refusing to change permissions on a broad system or user directory"
        ;;
esac

if [ -L "$key_dir" ]; then
    fail "key directory must not be a symbolic link"
fi
if [ -e "$key_dir" ] && [ ! -d "$key_dir" ]; then
    fail "key directory path exists but is not a directory"
fi

umask 077
if [ -d "$key_dir" ]; then
    case "$(uname -s)" in
        Darwin) key_dir_mode="$(stat -f '%Lp' "$key_dir")" ;;
        Linux) key_dir_mode="$(stat -c '%a' "$key_dir")" ;;
        *) fail "this script supports only macOS and Linux" ;;
    esac
    [ "$key_dir_mode" = "700" ] || fail "existing key directory must already have mode 0700"
else
    mkdir -p "$key_dir"
    chmod 700 "$key_dir"
fi
key_dir="$(CDPATH= cd -- "$key_dir" && pwd -P)"

# Resolve existing parent links before the final repository check. No PEM has
# been created at this point.
case "$key_dir" in
    "$repo_root"|"$repo_root"/*) fail "key directory resolves inside the MultiRAG repository" ;;
esac

private_key="$key_dir/$kid-private.pem"
public_key="$key_dir/$kid-public.pem"

for output_path in "$private_key" "$public_key"; do
    if [ -L "$output_path" ]; then
        fail "refusing to replace a symbolic link: $output_path"
    fi
    if [ -e "$output_path" ] && [ "$force" -eq 0 ]; then
        fail "$output_path already exists; use a new kid for rotation or pass --force"
    fi
done

private_tmp="$(mktemp "$key_dir/.${kid}.private.XXXXXX")"
public_tmp="$(mktemp "$key_dir/.${kid}.public.XXXXXX")"
derived_der="$(mktemp "$key_dir/.${kid}.derived.XXXXXX")"
public_der="$(mktemp "$key_dir/.${kid}.public-der.XXXXXX")"

cleanup() {
    rm -f "$private_tmp" "$public_tmp" "$derived_der" "$public_der"
}
trap cleanup EXIT HUP INT TERM

openssl genpkey \
    -algorithm EC \
    -pkeyopt ec_paramgen_curve:prime256v1 \
    -out "$private_tmp"
chmod 600 "$private_tmp"

openssl pkey \
    -in "$private_tmp" \
    -pubout \
    -out "$public_tmp"
chmod 644 "$public_tmp"

# Validate both PEM files and compare the exact DER-encoded public key. The
# comparison proves that the configured public key belongs to this private key.
openssl pkey -in "$private_tmp" -check -noout
openssl pkey -pubin -in "$public_tmp" -noout
openssl pkey -in "$private_tmp" -pubout -outform DER -out "$derived_der"
openssl pkey -pubin -in "$public_tmp" -outform DER -out "$public_der"

private_public_digest="$(openssl dgst -sha256 "$derived_der" | awk '{print $NF}')"
public_digest="$(openssl dgst -sha256 "$public_der" | awk '{print $NF}')"
[ "$private_public_digest" = "$public_digest" ] || fail "generated public key does not match private key"

# Each rename is atomic on this directory's filesystem. A new kid is the safe
# rotation path; --force exists only for disposable/test material.
mv -f "$public_tmp" "$public_key"
mv -f "$private_tmp" "$private_key"
chmod 644 "$public_key"
chmod 600 "$private_key"

# Prevent the EXIT trap from removing the completed files after the renames.
private_tmp=""
public_tmp=""

yaml_private="$(printf '%s' "$private_key" | sed "s/'/''/g")"
yaml_public="$(printf '%s' "$public_key" | sed "s/'/''/g")"

echo "Generated ES256 / P-256 signing keypair"
echo "kid: $kid"
echo "private key (MultiRAG only): $private_key"
echo "public key (published through JWKS): $public_key"
echo "public key SHA-256: $public_digest"
echo
echo "identity.mcp_issuer.key_provider:"
echo "  kind: file"
echo "  active_key_id: '$kid'"
echo "  private_key_file: '$yaml_private'"
echo "  public_key_files:"
echo "    '$kid': '$yaml_public'"
echo
echo "Do not commit either PEM file. Do not send the private key to of_mcp."
echo "See docs/enterprise-identity-mcp/P3_SIGNING_KEYS.md before enabling the issuer."
