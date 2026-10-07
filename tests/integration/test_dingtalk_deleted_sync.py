"""Controlled DingTalk SDK with actual SQL, MinIO, Milvus and API readback."""

from typing import Any

import pytest

from common.data_source.dingtalk_ai_table_connector import DingTalkAITableConnector
from core.svr.sync_data_source import DingTalkAITable
from tests.support.connector_deleted_sync import assert_configuration_readback, assert_deleted_sync
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.dingtalk_source import DingTalkSource
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.mark.parametrize("mode", ["complete", "empty", "listing-error", "malformed", "page-error", "ingestion-error"])
async def test_dingtalk_real_resources(connector_sync_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    source = DingTalkSource(mode)
    monkeypatch.setattr(DingTalkAITableConnector, "_create_client", lambda self: source)
    conf = {"table_id": "table", "operator_id": "operator", "batch_size": 1, "credentials": {"access_token": "synthetic"}, "sync_deleted_files": True}
    await assert_deleted_sync(connector_sync_api, monkeypatch, DingTalkAITable, "dingtalk_ai_table", {name: f"dingtalk_ai_table:table:sheet:{name}" for name in ["retained", "stale"]}, conf, mode)


def test_dingtalk_configuration_readback(connector_sync_api: dict[str, Any]) -> None:
    assert_configuration_readback(connector_sync_api, "dingtalk_ai_table", {"table_id": "table", "operator_id": "operator", "credentials": {"access_token": "synthetic"}, "sync_deleted_files": False})
