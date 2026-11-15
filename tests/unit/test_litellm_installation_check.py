"""Installation checks use synthetic dist-info trees, never mutate the venv."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts import check_litellm_installation as probe
from tests.support.resource_journal import ResourceJournal


@pytest.fixture
def installation(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    journal = ResourceJournal("litellm_installation_fixture")
    site = tmp_path / "site-packages"
    lock = tmp_path / "uv.lock"
    journal.event("reserve-paths", paths=[str(site), str(lock)])
    site.mkdir()
    lock.write_text('version=1\n[[package]]\nname="litellm"\nversion="1.82.5"\n')
    try:
        yield site, lock
    finally:
        shutil.rmtree(site)
        lock.unlink()
        assert not site.exists() and not lock.exists()
        journal.event("path-cleanup-readback", paths=[str(site), str(lock)], absent=True)


def add_distribution(site: Path, *, name: str = "litellm", version: str = "1.82.5", files: dict[str, bytes] | None = None) -> Path:
    directory = site / f"{name.replace('-', '_')}-{version}.dist-info"
    directory.mkdir()
    content = files if files is not None else {"litellm/__init__.py": b"# known package\n", "litellm/client.py": b"# client\n"}
    content = {**content, f"{directory.name}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()}
    rows = []
    for relative, body in content.items():
        target = site / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode()
        rows.append((relative, f"sha256={digest}", str(len(body))))
    rows.append((f"{directory.name}/RECORD", "", ""))
    with (directory / "RECORD").open("w", newline="") as record:
        csv.writer(record).writerows(rows)
    return directory


def test_complete_locked_installation_checks_all_recorded_files(installation: tuple[Path, Path]) -> None:
    site, lock = installation
    add_distribution(site)
    report = probe.check_installation(lock, site_packages=[site])
    assert report.version == "1.82.5"
    assert report.package_root == site / "litellm"
    assert report.files_verified == 4
    assert report.hashes_verified == 3


@pytest.mark.parametrize("relative", ["litellm/__init__.py", "litellm/client.py", "litellm-1.82.5.dist-info/METADATA"])
def test_record_missing_file_is_rejected(installation: tuple[Path, Path], relative: str) -> None:
    site, lock = installation
    add_distribution(site)
    (site / relative).unlink()
    with pytest.raises(probe.InstallationError):
        probe.check_installation(lock, site_packages=[site])


def test_tampered_file_is_rejected(installation: tuple[Path, Path]) -> None:
    site, lock = installation
    add_distribution(site)
    (site / "litellm/client.py").write_bytes(b"# changed after installation\n")
    with pytest.raises(probe.InstallationError, match="record_hash_mismatch"):
        probe.check_installation(lock, site_packages=[site])


@pytest.mark.parametrize("name", ["unclecode-litellm", "another-owner"])
@pytest.mark.parametrize("file_present", [True, False])
def test_same_import_namespace_owned_by_two_distributions_is_rejected(installation: tuple[Path, Path], name: str, file_present: bool) -> None:
    site, lock = installation
    add_distribution(site)
    add_distribution(site, name=name, version="1.0", files={"litellm/overlap.py": b"# second distribution\n"})
    if not file_present:
        (site / "litellm/overlap.py").unlink()
    with pytest.raises(probe.InstallationError, match=r"overlapping_fork|namespace_has_multiple"):
        probe.check_installation(lock, site_packages=[site])


def test_fork_metadata_without_files_cannot_evade_exclusion(installation: tuple[Path, Path]) -> None:
    site, lock = installation
    add_distribution(site)
    fork = add_distribution(site, name="unclecode-litellm", version="1.0", files={})
    (fork / "RECORD").unlink()
    with pytest.raises(probe.InstallationError, match="overlapping_fork_installed"):
        probe.check_installation(lock, site_packages=[site])


@pytest.mark.parametrize("broken", ["missing", "empty", "unhashed_namespace"])
def test_record_must_describe_hashed_package_files(installation: tuple[Path, Path], broken: str) -> None:
    site, lock = installation
    directory = add_distribution(site)
    record = directory / "RECORD"
    if broken == "missing":
        record.unlink()
    elif broken == "empty":
        record.write_text("")
    else:
        lines = list(csv.reader(record.read_text().splitlines()))
        lines[0][1] = ""
        with record.open("w", newline="") as output:
            csv.writer(output).writerows(lines)
    with pytest.raises(probe.InstallationError, match=r"record_missing_or_empty|namespace_record_hash_missing"):
        probe.check_installation(lock, site_packages=[site])


def test_installed_version_must_equal_lock(installation: tuple[Path, Path]) -> None:
    site, lock = installation
    add_distribution(site, version="1.84.0")
    with pytest.raises(probe.InstallationError, match="version_mismatch"):
        probe.check_installation(lock, site_packages=[site])


def test_ambiguous_lock_is_rejected(installation: tuple[Path, Path]) -> None:
    site, lock = installation
    add_distribution(site)
    with lock.open("a") as output:
        output.write('[[package]]\nname="litellm"\nversion="1.84.0"\n')
    with pytest.raises(probe.InstallationError, match="version_ambiguous"):
        probe.check_installation(lock, site_packages=[site])


def test_import_origin_is_rechecked_and_sensitive_stderr_is_not_reported(installation: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    site, lock = installation
    add_distribution(site)
    report = probe.check_installation(lock, site_packages=[site])
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"ok": True, "module_file": "/different/litellm/__init__.py", "model_cost_entries": 1, "network_attempts": 0}),
        stderr="credential must not be reported",
    )
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(probe.InstallationError, match="isolated_import_origin_mismatch") as refused:
        probe.check_import(report)
    assert "credential" not in str(refused.value)


def test_cli_never_imports_a_broken_installation(installation: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    site, lock = installation
    add_distribution(site)
    (site / "litellm/client.py").unlink()
    monkeypatch.setattr(probe.sysconfig, "get_paths", lambda: {"purelib": str(site), "platlib": str(site)})

    def refuse_launch(*args: object, **kwargs: object) -> None:
        raise AssertionError("Broken files reached the import subprocess")

    monkeypatch.setattr(probe.subprocess, "run", refuse_launch)
    assert probe.main(["--lock-file", str(lock)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "record_file_missing: litellm/client.py"
