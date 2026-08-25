"""Cross-platform operator tooling for EIM-O1 P3 signing keys."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from api.identity.mcp_issuer.keys import FileSigningKeyProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
POSIX_SCRIPT = REPO_ROOT / "scripts" / "init_mcp_signing_key.example.sh"
WINDOWS_SCRIPT = REPO_ROOT / "scripts" / "init_mcp_signing_key.example.ps1"


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None or shutil.which("openssl") is None,
    reason="POSIX key generation requires sh and OpenSSL",
)
def test_posix_script_generates_provider_compatible_p256_pair(tmp_path: Path) -> None:
    kid = "p3-test-2026-08"
    completed = subprocess.run(
        ["sh", str(POSIX_SCRIPT), "--kid", kid, "--key-dir", str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    private_path = tmp_path / f"{kid}-private.pem"
    public_path = tmp_path / f"{kid}-public.pem"
    assert private_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    assert public_path.read_bytes().startswith(b"-----BEGIN PUBLIC KEY-----")
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    assert "BEGIN PRIVATE KEY" not in completed.stdout
    assert "public key SHA-256:" in completed.stdout

    provider = FileSigningKeyProvider.from_files(
        active_key_id=kid,
        private_key_file=private_path,
        public_key_files={kid: public_path},
    )
    jwk = provider.snapshot.to_jwks_document()["keys"][0]
    assert jwk["kid"] == kid
    assert jwk["alg"] == "ES256"
    assert jwk["crv"] == "P-256"


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None or shutil.which("openssl") is None,
    reason="POSIX key generation requires sh and OpenSSL",
)
def test_posix_script_refuses_invalid_kid_and_existing_key(tmp_path: Path) -> None:
    invalid = subprocess.run(
        ["sh", str(POSIX_SCRIPT), "--kid", "../escape", "--key-dir", str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert invalid.returncode != 0
    assert list(tmp_path.iterdir()) == []

    inside_repository = REPO_ROOT / ".p3-key-test-must-not-exist"
    repository_attempt = subprocess.run(
        ["sh", str(POSIX_SCRIPT), "--kid", "p3-repository", "--key-dir", str(inside_repository)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert repository_attempt.returncode != 0
    assert "outside the MultiRAG repository" in repository_attempt.stderr
    assert not inside_repository.exists()

    permissive_directory = tmp_path / "permissive"
    permissive_directory.mkdir(mode=0o755)
    permissive_attempt = subprocess.run(
        ["sh", str(POSIX_SCRIPT), "--kid", "p3-permissive", "--key-dir", str(permissive_directory)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert permissive_attempt.returncode != 0
    assert "mode 0700" in permissive_attempt.stderr
    assert stat.S_IMODE(permissive_directory.stat().st_mode) == 0o755

    command = ["sh", str(POSIX_SCRIPT), "--kid", "p3-existing", "--key-dir", str(tmp_path)]
    first = subprocess.run(command, capture_output=True, check=False, text=True)
    private_before = (tmp_path / "p3-existing-private.pem").read_bytes()
    second = subprocess.run(command, capture_output=True, check=False, text=True)

    assert first.returncode == 0, first.stderr
    assert second.returncode != 0
    assert "already exists" in second.stderr
    assert (tmp_path / "p3-existing-private.pem").read_bytes() == private_before


def test_windows_script_contains_equivalent_generation_and_safety_guards() -> None:
    script = WINDOWS_SCRIPT.read_text(encoding="utf-8")

    for required_contract in (
        "ec_paramgen_curve:prime256v1",
        "Get-FileHash",
        "IsPathRooted",
        "ReparsePoint",
        "PrivateKeyReader",
        "icacls.exe",
        "/inheritance:r",
        "genpkey",
        "outside the MultiRAG repository",
        "filesystem root",
        "unmanaged entry",
    ):
        assert required_contract in script

    assert "BEGIN PRIVATE KEY-----" not in script
    assert "ConvertTo-YamlSingleQuoted" in script
