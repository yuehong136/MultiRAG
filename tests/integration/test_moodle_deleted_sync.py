"""Actual isolated persistence/storage with only Moodle REST controlled."""

from typing import Any

import pytest

from core.svr.sync_data_source import Moodle
from tests.support.connector_deleted_sync import assert_configuration_readback, assert_deleted_sync
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.moodle_source import MoodleSource
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.mark.parametrize("mode", ["complete", "empty", "listing-error", "course-error", "malformed", "ingestion-error", "disabled", "download-error"])
async def test_moodle_real_resources(connector_sync_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    source = MoodleSource(mode)
    source.modules = source.modules[:1]
    if mode in {"ingestion-error", "download-error"}:
        source.modules[0]["timemodified"] = 1893456000 if mode == "download-error" else 0
    if mode == "download-error":
        source.modules[0] = {"id": 11, "name": "File", "modname": "resource", "timemodified": 0, "contents": [{"filename": "file.txt", "fileurl": "https://moodle.invalid/file"}]}

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("controlled download failure")

        monkeypatch.setattr("common.data_source.moodle_connector.rl_requests.get", fail)
    monkeypatch.setattr("common.data_source.moodle_connector.MoodleClient", lambda *args: source)
    conf = {"moodle_url": "https://moodle.invalid", "credentials": {"moodle_token": "synthetic-secret"}, "sync_deleted_files": mode != "disabled", "batch_size": 1}
    await assert_deleted_sync(connector_sync_api, monkeypatch, Moodle, "moodle", {"retained": "moodle_assign_11", "stale": "moodle_quiz_12"}, conf, mode)


def test_moodle_configuration_readback(connector_sync_api: dict[str, Any]) -> None:
    assert_configuration_readback(connector_sync_api, "moodle", {"moodle_url": "https://moodle.invalid", "credentials": {"moodle_token": "synthetic"}, "sync_deleted_files": False})
