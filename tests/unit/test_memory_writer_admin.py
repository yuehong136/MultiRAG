"""Recovery declarations fail before importing the application without exact inputs."""

import argparse

import pytest

from scripts import memory_writer_admin


@pytest.mark.parametrize("action", ["attest-legacy", "attest-attempt"])
def test_admin_rejects_incomplete_attestation_before_application_import(action: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(options: argparse.Namespace) -> None:
        raise AssertionError("Incomplete attestation reached the database")

    monkeypatch.setattr(memory_writer_admin, "execute", forbidden)
    monkeypatch.setenv("MULTIRAG_RUN_ID", "owned-unit-run")
    with pytest.raises(SystemExit) as error:
        memory_writer_admin.main([action, "--memory-id", "bank", "--tenant-id", "tenant", "--confirm-all-selected-io-settled"])
    assert error.value.code == 2


def test_admin_requires_registered_runtime_even_for_read_only_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(options: argparse.Namespace) -> None:
        raise AssertionError("Unregistered command imported the application")

    monkeypatch.setattr(memory_writer_admin, "execute", forbidden)
    monkeypatch.delenv("MULTIRAG_RUN_ID", raising=False)
    with pytest.raises(SystemExit) as error:
        memory_writer_admin.main(["audit", "--memory-id", "bank", "--tenant-id", "tenant"])
    assert error.value.code == 2
