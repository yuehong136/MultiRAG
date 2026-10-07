from typing import Any

import pytest

from common.data_source.dingtalk_ai_table_connector import DingTalkAITableConnector
from common.data_source.interfaces import collect_slim_document_snapshot
from core.svr import sync_data_source as sync_module
from tests.support.dingtalk_source import DingTalkSource
from tests.unit.test_sync_deleted_snapshot_contract import sync_env as sync_env
from tests.unit.test_sync_deleted_snapshot_contract import task


def connector(mode: str = "complete") -> DingTalkAITableConnector:
    result = DingTalkAITableConnector("table", "operator", batch_size=1)
    result._client = DingTalkSource(mode)  # type: ignore[assignment]
    return result


def test_snapshot_matches_content_all_sheets_and_pages_without_blob_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    source = connector("multi")
    content = [doc.id for batch in source.poll_source(0, 1) for doc in batch]
    assert content == ["dingtalk_ai_table:table:sheet:retained", "dingtalk_ai_table:table:other:retained"]
    monkeypatch.setattr(source, "_convert_record_to_document", lambda *args, **kwargs: pytest.fail("slim must not build blobs"))
    assert [doc.id for doc in collect_slim_document_snapshot(source)] == content
    assert source._client.calls == [("sheet", ""), ("sheet", "page-2"), ("other", "")] * 2


@pytest.mark.parametrize("mode", ["sheets-error", "listing-error", "page-error", "malformed", "invalid-id", "invalid-fields", "cycle"])
def test_partial_invalid_and_cyclic_listings_cannot_publish_snapshot(mode: str) -> None:
    with pytest.raises((PermissionError, RuntimeError, ValueError)):
        collect_slim_document_snapshot(connector(mode))


def test_successful_empty_table_is_authoritative() -> None:
    assert collect_slim_document_snapshot(connector("empty")) == ()


@pytest.mark.parametrize("mode", ["complete", "empty", "listing-error", "disabled", "first", "reindex"])
async def test_driver_reuses_complete_snapshot_coordinator(sync_env: dict[str, Any], mode: str) -> None:
    source = DingTalkSource(mode)
    sync_env["monkeypatch"].setattr(DingTalkAITableConnector, "_create_client", lambda self: source)
    sync_env["monkeypatch"].setattr(sync_module.SyncLogsService, "duplicate_and_parse", lambda *args: ([], []))
    driver = sync_module.DingTalkAITable(
        {"table_id": "table", "operator_id": "operator", "batch_size": "invalid", "credentials": {"access_token": "synthetic"}, "sync_deleted_files": mode != "disabled"}
    )
    current = task()
    if mode == "first":
        current["poll_range_start"] = None
    elif mode == "reindex":
        current["reindex"] = "1"
    await driver(current)
    calls = sync_env["calls"]
    assert calls[0] == "start"
    assert calls[-1] == ("fail" if mode == "listing-error" else "complete")
    assert any(isinstance(call, tuple) and call[0] == "cleanup" for call in calls) == (mode in {"complete", "empty"})
