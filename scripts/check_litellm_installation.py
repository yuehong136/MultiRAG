#!/usr/bin/env python3
"""Check the locked LiteLLM installation without repairing the environment.

RECORD checks detect incomplete or overlapping installations. They do not
establish that a release is free of vulnerabilities or validate providers.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import sysconfig
import tomllib
from dataclasses import dataclass
from importlib.metadata import Distribution, PackagePath, distributions
from pathlib import Path
from typing import Any


class InstallationError(ValueError):
    """An installation cannot satisfy the locked package contract."""


@dataclass(frozen=True)
class InstallationReport:
    version: str
    package_root: Path
    files_verified: int
    hashes_verified: int


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_version(lock_file: Path) -> str:
    """Read an unambiguous exact LiteLLM version from the project lock."""
    try:
        data = tomllib.loads(lock_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise InstallationError("lock_unreadable") from error
    versions = {row["version"] for row in data.get("package", []) if row.get("name") == "litellm" and isinstance(row.get("version"), str)}
    if len(versions) != 1:
        raise InstallationError("lock_litellm_version_ambiguous")
    return versions.pop()


def _record_rows(distribution: Distribution) -> list[list[str]]:
    # Distribution.files filters nonexistent paths on some supported Python
    # releases. Read the raw inventory so missing files cannot disappear here.
    try:
        return list(csv.reader(io.StringIO(distribution.read_text("RECORD") or "")))
    except (OSError, csv.Error) as error:
        raise InstallationError("record_unreadable") from error


def _owns_namespace(distribution: Distribution) -> bool:
    rows = _record_rows(distribution)
    if any(row and PackagePath(row[0]).parts and PackagePath(row[0]).parts[0] == "litellm" for row in rows):
        return True
    return "litellm" in (distribution.read_text("top_level.txt") or "").split()


def check_installation(lock_file: Path, *, site_packages: list[Path] | None = None) -> InstallationReport:
    """Verify distribution ownership and all recorded files without importing it."""
    expected = locked_version(lock_file)
    if site_packages is None:
        paths = sysconfig.get_paths()
        site_packages = list(dict.fromkeys(Path(paths[key]) for key in ("purelib", "platlib")))
    installed = list(distributions(path=[str(path) for path in site_packages]))
    upstream = [item for item in installed if _canonical_name(item.metadata.get("Name", "")) == "litellm"]
    if any(_canonical_name(item.metadata.get("Name", "")) == "unclecode-litellm" for item in installed):
        raise InstallationError("overlapping_fork_installed")
    if len(upstream) != 1:
        raise InstallationError("litellm_distribution_missing_or_duplicated")
    distribution = upstream[0]
    if distribution.version != expected:
        raise InstallationError(f"version_mismatch: expected {expected}, found {distribution.version}")
    recorded = _record_rows(distribution)
    if not recorded:
        raise InstallationError("record_missing_or_empty")
    if any(len(row) != 3 or not row[0] for row in recorded):
        raise InstallationError("record_malformed")
    owners = [item for item in installed if _owns_namespace(item)]
    if len(owners) != 1 or owners[0] is not distribution:
        raise InstallationError("litellm_namespace_has_multiple_or_missing_owners")
    package_root = Path(str(distribution.locate_file(PackagePath("litellm")))).resolve()
    if not any(row[0] == "litellm/__init__.py" for row in recorded) or not (package_root / "__init__.py").is_file():
        raise InstallationError("package_initializer_missing")
    hashes_verified = 0
    for filename, encoded_hash, _ in recorded:
        file = PackagePath(filename)
        path = Path(str(distribution.locate_file(file)))
        if not path.is_file():
            raise InstallationError(f"record_file_missing: {file}")
        namespace_file = bool(file.parts and file.parts[0] == "litellm")
        if namespace_file and not path.resolve().is_relative_to(package_root):
            raise InstallationError(f"namespace_file_outside_package: {file}")
        if not encoded_hash:
            if namespace_file:
                raise InstallationError(f"namespace_record_hash_missing: {file}")
            continue
        algorithm, separator, expected_hash = encoded_hash.partition("=")
        if not separator or not algorithm or not expected_hash:
            raise InstallationError(f"record_hash_malformed: {file}")
        try:
            digest = hashlib.new(algorithm)
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        except (OSError, ValueError) as error:
            raise InstallationError(f"record_file_unreadable: {file}") from error
        actual = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")
        if actual != expected_hash:
            raise InstallationError(f"record_hash_mismatch: {file}")
        hashes_verified += 1
    return InstallationReport(expected, package_root, len(recorded), hashes_verified)


_IMPORT_CHECK = """
import json
import socket
import sys
from pathlib import Path

attempts = []
def deny_network(*args: object, **kwargs: object) -> None:
    attempts.append(True)
    raise RuntimeError("dependency_probe_network_forbidden")

socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network
socket.getaddrinfo = deny_network

try:
    import litellm
    from litellm import completion, acompletion, get_llm_provider
    expected = Path(sys.argv[1]).resolve()
    module_file = Path(litellm.__file__).resolve()
    if not module_file.is_relative_to(expected):
        raise RuntimeError("dependency_probe_import_origin_mismatch")
    if not all(callable(item) for item in (completion, acompletion, get_llm_provider)):
        raise RuntimeError("dependency_probe_api_missing")
    if not isinstance(litellm.model_cost, dict) or not litellm.model_cost:
        raise RuntimeError("dependency_probe_cost_map_empty")
    if attempts:
        raise RuntimeError("dependency_probe_attempted_network")
    print(json.dumps({"ok": True, "module_file": str(module_file), "model_cost_entries": len(litellm.model_cost), "network_attempts": len(attempts)}))
except Exception as error:
    # Import hooks may include configuration in exception messages. Report the
    # class, never library stderr or credential-bearing exception text.
    print(json.dumps({"ok": False, "error_type": type(error).__name__, "network_attempts": len(attempts)}))
    raise SystemExit(1)
"""


def check_import(report: InstallationReport, *, timeout: float = 60) -> dict[str, Any]:
    """Use this interpreter in isolated mode and reject any network attempt."""
    command = [sys.executable, "-I", "-B", "-c", _IMPORT_CHECK, str(report.package_root)]
    journal = os.environ.get("MULTIRAG_RUN_JOURNAL")
    if journal:
        command = [sys.executable, "-B", str(Path(__file__).with_name("run_registered.py")), "--child", journal, *command]
    environment = {**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    try:
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallationError(f"isolated_import_unconfirmed: {type(error).__name__}") from error
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as error:
        raise InstallationError(f"isolated_import_unconfirmed: exit {result.returncode}") from error
    if result.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
        error_type = payload.get("error_type") if isinstance(payload, dict) else None
        raise InstallationError(f"isolated_import_failed: {error_type or 'unknown'}; exit {result.returncode}")
    module_file = payload.get("module_file")
    if not isinstance(module_file, str) or not Path(module_file).resolve().is_relative_to(report.package_root):
        raise InstallationError("isolated_import_origin_mismatch")
    if payload.get("network_attempts") != 0 or not isinstance(payload.get("model_cost_entries"), int) or payload["model_cost_entries"] <= 0:
        raise InstallationError("isolated_import_contract_failed")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-file", type=Path, default=Path(__file__).resolve().parents[1] / "uv.lock")
    options = parser.parse_args(argv)
    try:
        report = check_installation(options.lock_file)
        imported = check_import(report)
    except InstallationError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "version": report.version,
                "files_verified": report.files_verified,
                "hashes_verified": report.hashes_verified,
                "import": imported,
                "scope": "installation and offline import only; no vulnerability or provider certification",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
