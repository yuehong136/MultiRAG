from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from common.data_source.models import ConnectorFailure, EntityFailure, SlimDocument
from core.svr import sync_data_source as sync_module
from tests.unit.test_sync_deleted_snapshot_contract import sync_env as sync_env
from tests.unit.test_sync_deleted_snapshot_contract import task


class Source:
    primary_admin_email = "user@example.test"

    def __init__(self, *, failure: bool = False, rotated: bool = False) -> None:
        self.failure = failure
        self.rotated = rotated
        self.listed = False
        self.windows: list[tuple[float, float]] = []
        self.snapshot_started: float | None = None

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        return {**credentials, "access_token": "rotated"} if self.rotated else None

    def set_allow_images(self, value: bool) -> None:
        pass

    def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Iterator[list[SlimDocument]]:
        self.listed = True
        self.snapshot_started = datetime.now(UTC).timestamp()
        yield [SlimDocument(id="remote-1")]

    def load_from_state(self) -> Iterator[list[Any]]:
        return iter(())

    def poll_source(self, start: float, end: float) -> Iterator[list[Any]]:
        self.windows.append((start, end))

        def batches() -> Iterator[list[Any]]:
            if self.failure:
                raise PermissionError("Airtable content failed")
            yield from ()

        return batches()

    def build_dummy_checkpoint(self) -> Any:
        return SimpleNamespace(has_more=True)

    def load_from_checkpoint(self, start: float, end: float, checkpoint: Any) -> Generator[Any, None, Any]:
        self.windows.append((start, end))
        if self.failure:
            yield ConnectorFailure(failure_message="mapping failed", failed_entity=EntityFailure(entity_id="remote-1"))
        return SimpleNamespace(has_more=False)


@pytest.mark.parametrize(
    "driver_type,constructor",
    [(sync_module.Airtable, "AirtableConnector"), (sync_module.GoogleDrive, "GoogleDriveConnector"), (sync_module.Gmail, "GmailConnector"), (sync_module.Bitbucket, "BitbucketConnector")],
)
@pytest.mark.parametrize("mode", ["incremental", "disabled", "first", "reindex", "content-failure"])
async def test_real_driver_deletion_gate_and_failed_ingestion(sync_env: dict[str, Any], driver_type: Any, constructor: str, mode: str) -> None:
    monkeypatch = sync_env["monkeypatch"]
    source = Source(failure=mode == "content-failure")
    monkeypatch.setattr(sync_module, constructor, lambda **kwargs: source)
    driver = driver_type({"sync_deleted_files": mode != "disabled", "credentials": {"airtable_access_token": "test", "bitbucket_account_email": "user@example.test", "bitbucket_api_token": "test"}})
    current = task()
    if mode == "first":
        current["poll_range_start"] = None
    elif mode == "reindex":
        current["reindex"] = "1"
    await driver(current)
    assert source.listed == (mode in {"incremental", "content-failure"})
    if mode == "content-failure":
        assert sync_env["calls"] == ["start", "fail"]
    elif mode == "incremental":
        assert sync_env["calls"][1][0] == "cleanup" and sync_env["calls"][-1] == "complete"
        assert source.snapshot_started is not None and source.windows[0][1] <= source.snapshot_started
    else:
        assert sync_env["calls"] == ["start", "complete"]


@pytest.mark.parametrize("driver_type,constructor", [(sync_module.GoogleDrive, "GoogleDriveConnector"), (sync_module.Gmail, "GmailConnector")])
async def test_google_rotated_credentials_persist_with_existing_configuration(sync_env: dict[str, Any], driver_type: Any, constructor: str) -> None:
    monkeypatch = sync_env["monkeypatch"]
    source = Source(rotated=True)
    writes: list[Any] = []
    monkeypatch.setattr(sync_module, constructor, lambda **kwargs: source)
    monkeypatch.setattr(sync_module.ConnectorService, "update_by_id", lambda *args: writes.append(args[-1]))
    driver = driver_type({"sync_deleted_files": True, "include_my_drives": True, "custom": "keep", "credentials": {"refresh_token": "keep"}})
    await driver(task())
    assert writes == [{"config": {"sync_deleted_files": True, "include_my_drives": True, "custom": "keep", "credentials": {"refresh_token": "keep", "access_token": "rotated"}}}]
