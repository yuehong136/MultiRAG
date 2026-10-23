"""File consumers restore authority even when entry preflight is bypassed."""

import json
from typing import Any

import pytest

from agent.canvas import Canvas
from agent.component.base import ComponentBase
from api.db.services.file_service import FileService, RuntimeAttachmentError
from common import settings
from tests.support.agent_update_release import message_dsl
from tests.unit.test_runtime_document_upload import RuntimeStorage


@pytest.fixture
def runtime_uploads(monkeypatch: pytest.MonkeyPatch) -> tuple[RuntimeStorage, dict[str, Any], dict[str, Any]]:
    storage = RuntimeStorage()
    monkeypatch.setattr(settings, "STORAGE_IMPL", storage)
    descriptors = []
    for owner, location in [("caller", "a" * 32), ("resource-owner", "b" * 32)]:
        descriptor = {"id": location, "created_by": owner, "name": "actual.csv", "mime_type": "text/csv", "extension": "csv", "size": 12, "created_at": 1, "preview_url": None}
        storage.objects[f"{owner}-downloads", location] = b"value\nowned\n"
        storage.objects[f"{owner}-downloads", f"{location}.upload.json"] = json.dumps(descriptor).encode()
        descriptors.append(descriptor)
    monkeypatch.setattr(ComponentBase, "check_if_canceled", lambda *_: False)
    return storage, descriptors[0], descriptors[1]


@pytest.mark.parametrize("source", ["Begin", "UserFillUp"])
def test_direct_component_file_input_uses_caller_namespace(runtime_uploads: tuple[RuntimeStorage, dict[str, Any], dict[str, Any]], monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    _, own, foreign = runtime_uploads
    dsl = message_dsl("answer")
    dsl["components"]["begin"]["obj"]["component_name"] = source
    canvas = Canvas(json.dumps(dsl), "caller")
    parsed = []
    monkeypatch.setattr(FileService, "parse", lambda name, blob, raw, owner, layout: parsed.append((name, owner, blob)) or "parsed")
    component = canvas.get_component_obj("begin")
    component._invoke(inputs={"undeclared": {"type": "file", "value": {**own, "created_by": "resource-owner", "name": "fake.pdf"}}})
    assert component.output("undeclared") == ["parsed"]
    assert parsed == [("actual.csv", "caller", b"value\nowned\n")]
    with pytest.raises(RuntimeAttachmentError):
        component._invoke(inputs={"undeclared": {"type": "file", "value": foreign}})
    assert len(parsed) == 1
    canvas._thread_pool.shutdown()


@pytest.mark.parametrize(
    "ref,value",
    [
        ("{begin@sheet}", {"id": "b" * 32}),
        ("{begin@sheet.payload}", json.dumps({"payload": {"file_id": "b" * 32}})),
        ("begin@sheet.-1", [{"id": "b" * 32}]),
        ("{begin@sheet}", [[{"id": "b" * 32}]]),
    ],
)
def test_excel_sink_refuses_foreign_files_after_dynamic_or_direct_invocation(
    runtime_uploads: tuple[RuntimeStorage, dict[str, Any], dict[str, Any]], monkeypatch: pytest.MonkeyPatch, ref: str, value: Any
) -> None:
    storage, _, _ = runtime_uploads
    dsl = message_dsl("answer")
    dsl["components"]["Excel:read"] = {"obj": {"component_name": "ExcelProcessor", "params": {}}, "upstream": [], "downstream": []}
    canvas = Canvas(json.dumps(dsl), "caller")
    canvas.get_component_obj("begin").set_output("sheet", value)
    reads = []
    original = storage.get
    monkeypatch.setattr(storage, "get", lambda bucket, key: reads.append((bucket, key)) or original(bucket, key))
    with pytest.raises(RuntimeAttachmentError):
        canvas.get_component_obj("Excel:read")._get_file_content(ref)
    assert reads == []
    canvas._thread_pool.shutdown()


async def test_preflight_preserves_non_file_business_objects(runtime_uploads: tuple[RuntimeStorage, dict[str, Any], dict[str, Any]]) -> None:
    _, _, foreign = runtime_uploads
    inputs = {"business": {"type": "object", "value": {**foreign, "rows": [{"id": "record", "name": "sale"}]}}, "text": {"type": "string", "value": json.dumps(foreign)}}
    files, restored = await FileService.resolve_agent_runtime_inputs("caller", [], inputs, dsl=message_dsl("answer"))
    assert files == [] and restored == inputs
