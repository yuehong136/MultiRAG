"""ID-only MCP files become owner-scoped parsed input at the real model boundary."""

import json
import sys
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from api.db.services.user_service import TenantService
from common import settings
from tests.runtime_attachment_support import capture_chat_model, sse_frames
from tests.unit.test_runtime_document_upload import RuntimeStorage


@pytest.fixture
def attachment_chat(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    storage = RuntimeStorage()
    monkeypatch.setattr(settings, "STORAGE_IMPL", storage)
    monkeypatch.setattr(DocumentService, "check_doc_health", classmethod(lambda *_args: True))
    monkeypatch.setattr(TenantService, "get_info_by", classmethod(lambda *_args: [{"tenant_id": "tenant-unit"}]))
    # Unit-only parser boundary; integration uses real parsing and MinIO.
    monkeypatch.setattr(FileService, "parse", staticmethod(lambda name, blob, *_args: f"File {name}: {blob.decode()}"))
    captured = capture_chat_model(monkeypatch)
    body = client.post("/api/v1/documents/upload", files=[("file", ("note.txt", b"attachment secret", "text/plain")), ("file", ("image.png", b"png bytes", "image/png"))]).json()
    assert body["code"] == 0
    return {"storage": storage, "model": captured, "descriptors": body["data"]}


def chat_request(ids: list[str], *, stream: bool = True, structured: bool = False) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": "Read the attachments"}], "llm_name": "capture-model", "files": ids, "stream": stream, "structured_output": structured}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("tools", [False, True])
def test_all_chat_branches_receive_parsed_text_and_images(client: TestClient, attachment_chat: dict[str, Any], stream: bool, structured: bool, tools: bool) -> None:
    state = attachment_chat["model"]
    state["tools"] = tools
    ids = [item["id"] for item in attachment_chat["descriptors"]]
    response = client.post("/v1/llm/enhanced_chat_sse", json=chat_request(ids, stream=stream, structured=structured))
    assert response.status_code == 200, response.text
    if stream:
        frames = sse_frames(response.text)
        assert frames[-1]["retcode"] == 0 and frames[-1]["data"] is True
        assert all(frame["retcode"] == 0 for frame in frames)
    else:
        expected = "🔧 Starting tool analysis...\ncaptured reply" if tools and not structured else "captured reply"
        assert response.json()["retcode"] == 0 and response.json()["data"]["answer"] == expected
    assert len(state["calls"]) == 1
    call = state["calls"][0]
    assert "attachment secret" in str(call["history"])
    assert "note.txt" in str(call["history"]) and "Read the attachments" in str(call["history"])
    assert call["images"] == ["data:image/png;base64,cG5nIGJ5dGVz"]
    assert not any(location in str(call) for location in ids)


@pytest.mark.parametrize("stream,structured", [(True, False), (True, True), (False, False), (False, True)])
@pytest.mark.parametrize("failure", ["missing", "foreign", "missing_blob", "invalid_descriptor", "parse"])
def test_attachment_errors_are_rejected_before_streaming(client: TestClient, attachment_chat: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stream: bool, structured: bool, failure: str) -> None:
    descriptor = attachment_chat["descriptors"][0]
    storage = attachment_chat["storage"]
    location = descriptor["id"]
    if failure == "missing":
        location = uuid4().hex
    elif failure == "foreign":
        for key in (location, f"{location}.upload.json"):
            storage.objects["foreign-downloads", key] = storage.objects.pop(("user-unit-downloads", key))
    elif failure == "missing_blob":
        storage.objects.pop(("user-unit-downloads", location))
    elif failure == "invalid_descriptor":
        storage.objects["user-unit-downloads", f"{location}.upload.json"] = json.dumps({**descriptor, "created_by": "foreign"}).encode()
    elif failure == "parse":

        def failed_parse(*_args: Any) -> str:
            raise ValueError("malformed file")

        monkeypatch.setattr(FileService, "parse", staticmethod(failed_parse))
    request = chat_request([location], stream=stream, structured=structured)
    request["created_by"] = "foreign"  # Untrusted extra input cannot select the owner.
    response = client.post("/v1/llm/enhanced_chat_sse", json=request)
    assert response.status_code == 400 and response.json()["detail"]
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert attachment_chat["model"]["calls"] == []


@pytest.mark.parametrize("stream,structured", [(True, False), (True, True), (False, False), (False, True)])
@pytest.mark.parametrize("failure", ["exception", "marker"])
def test_model_failure_never_sends_success_completion(client: TestClient, attachment_chat: dict[str, Any], stream: bool, structured: bool, failure: str) -> None:
    attachment_chat["model"]["failure"] = failure
    response = client.post("/v1/llm/enhanced_chat_sse", json=chat_request([], stream=stream, structured=structured))
    if stream:
        assert response.status_code == 200
        frames = sse_frames(response.text)
        assert frames[-1]["retcode"] == 500
        assert not any(frame.get("data") is True for frame in frames)
    else:
        assert response.status_code == 500 and "detail" in response.json()


async def test_reused_adapter_clears_images_text_and_missing_attachment_state(attachment_chat: dict[str, Any]) -> None:
    module = sys.modules["api.apps.llm"]
    adapter = module.ChatAgentAdapter("tenant-unit", "capture-model", upload_owner_id="user-unit")
    ids = [item["id"] for item in attachment_chat["descriptors"]]
    try:
        await adapter.chat_async("first", files=ids)
        await adapter.chat_structured_async("second", files=[])
        second = attachment_chat["model"]["calls"][-1]
        assert "attachment secret" not in str(second) and second["images"] == []
        assert adapter.canvas_mock.globals["sys.files"] == []
        await adapter.chat_structured_async("third", files=ids)
        attachment_chat["storage"].objects.pop(("user-unit-downloads", ids[0]))
        with pytest.raises(ValueError, match=r"missing|unavailable"):
            await adapter.chat_async("fourth", files=ids)
        assert adapter.canvas_mock.globals["sys.files"] == [] and adapter.agent.imgs == []
        await adapter.chat_async("fifth")
        assert "attachment secret" not in str(attachment_chat["model"]["calls"][-1])
    finally:
        adapter.canvas_mock._thread_pool.shutdown()


def test_metadata_registration_failure_compensates_blob(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class FailedRegistration(RuntimeStorage):
        def put(self, bucket: str, key: str, blob: bytes) -> bool:
            if key.endswith(".upload.json"):
                return False
            return super().put(bucket, key, blob)

    storage = FailedRegistration()
    storage.objects["user-unit-downloads", "history"] = b"existing"
    monkeypatch.setattr(settings, "STORAGE_IMPL", storage)
    monkeypatch.setattr(DocumentService, "check_doc_health", classmethod(lambda *_args: True))
    response = client.post("/api/v1/documents/upload", files={"file": ("registration.txt", b"registered")})
    assert response.json()["code"] == 100 and "data" not in response.json()
    assert storage.objects == {("user-unit-downloads", "history"): b"existing"}
