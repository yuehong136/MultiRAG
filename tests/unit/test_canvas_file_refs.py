"""Raw per-turn file references coexist with existing parsed file content."""

import json
from typing import Any

import pytest

from agent.canvas import Canvas
from core.utils.redis_conn import REDIS_CONN
from tests.support.canvas_path_reset import paused_dsl


async def test_file_refs_keep_ids_only_and_clear_on_next_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Canvas, "is_canceled", lambda self: False)
    monkeypatch.setattr(REDIS_CONN, "delete", lambda *args: None)
    dsl = paused_dsl()
    dsl["components"]["Message:draft"]["downstream"] = []
    canvas = Canvas(json.dumps(dsl), tenant_id="unit-owner")
    supplied = [{"id": "owned-runtime", "name": "untrusted-name", "created_by": "foreign"}]

    async def parsed_files(self: Canvas, files: list[dict[str, Any]], layout_recognize: str | None = None) -> list[str]:
        assert files == supplied
        return ["parsed file text"]

    monkeypatch.setattr(Canvas, "get_files_async", parsed_files)
    try:
        first = [event async for event in canvas.run(query="first", files=supplied)]
        assert first[-1]["event"] == "workflow_finished"
        assert canvas.get_variable_value("sys.file_refs") == [{"id": "owned-runtime"}]
        assert canvas.get_variable_value("sys.files") == ["parsed file text"]
        assert supplied[0]["name"] == "untrusted-name"
        second = [event async for event in canvas.run(query="second")]
        assert second[-1]["event"] == "workflow_finished"
        assert canvas.get_variable_value("sys.file_refs") == []
        assert canvas.get_variable_value("sys.files") == ["parsed file text"]
    finally:
        canvas._thread_pool.shutdown(wait=True)
