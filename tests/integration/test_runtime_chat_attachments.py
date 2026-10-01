"""Real upload HTTP -> IDs -> owner registry -> MinIO/Canvas -> model capture."""

import base64
import json
from io import BytesIO
from typing import Any
from uuid import uuid4

import pytest
import requests
from PIL import Image

from tests.integration.test_runtime_document_upload import read_object
from tests.integration.test_runtime_document_upload import runtime_upload_api as runtime_upload_api
from tests.runtime_attachment_support import capture_chat_model, sse_frames


def test_real_http_mcp_attachment_consumption_and_failures(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = runtime_upload_api
    state = capture_chat_model(monkeypatch)
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    foreign_headers = {"Authorization": f"Bearer {env['api_key']}"}
    upload = f"{env['base']}/api/v1/documents/upload"
    chat = f"{env['base']}/v1/llm/enhanced_chat_sse"
    text = b"MCP runtime upload acceptance text 61a24a2c"
    png = BytesIO()
    Image.new("RGB", (4, 3), "green").save(png, format="PNG")
    image = png.getvalue()
    body = requests.post(upload, headers=headers, files=[("file", ("mcp-notes.txt", text, "text/plain")), ("file", ("mcp.png", image, "image/png"))], timeout=30).json()
    assert body["code"] == 0 and isinstance(body["data"], list)
    descriptors = body["data"]
    ids = [item["id"] for item in descriptors]
    for descriptor, binary in zip(descriptors, [text, image], strict=True):
        key = f"{env['owners'][0]}-downloads/{descriptor['id']}"
        assert read_object(env["storage"], env["bucket"], key) == binary
        assert json.loads(read_object(env["storage"], env["bucket"], f"{key}.upload.json")) == descriptor

    def request(file_ids: list[str], stream: bool, structured: bool, auth: dict[str, str] | None = None) -> requests.Response:
        return requests.post(
            chat,
            headers=auth or headers,
            json={"messages": [{"role": "user", "content": "Read attached notes and image"}], "llm_name": "capture-model", "files": file_ids, "stream": stream, "structured_output": structured},
            timeout=30,
        )

    for tools in (False, True):
        state["tools"] = tools
        for stream, structured in ((True, False), (True, True), (False, False), (False, True)):
            before = len(state["calls"])
            response = request(ids, stream, structured)
            assert response.status_code == 200, response.text
            if stream:
                frames = sse_frames(response.text)
                assert frames[-1]["retcode"] == 0 and frames[-1]["data"] is True
                assert all(frame["retcode"] == 0 for frame in frames)
            else:
                expected = "🔧 Starting tool analysis...\ncaptured reply" if tools and not structured else "captured reply"
                assert response.json()["retcode"] == 0 and response.json()["data"]["answer"] == expected
            assert len(state["calls"]) == before + 1
            model_input = state["calls"][-1]
            assert text.decode() in str(model_input["history"]) and "mcp-notes.txt" in str(model_input["history"])
            assert model_input["images"] == ["data:image/png;base64," + base64.b64encode(image).decode()]
            assert not any(location in str(model_input) for location in ids)

    # All active upload gateways register descriptors that ID-only MCP can restore.
    for path, field, auth, code_key in (("/v1/document/upload_info", "file", headers, "retcode"), ("/api/v1/files/upload_info", "files", foreign_headers, "code")):
        compat = requests.post(f"{env['base']}{path}", headers=auth, files={field: ("compat-mcp.txt", b"compat MCP content", "text/plain")}, timeout=30).json()
        assert compat[code_key] == 0
        response = request([compat["data"]["id"]], False, False, auth)
        assert response.status_code == 200 and response.json()["retcode"] == 0
        assert "compat MCP content" in str(state["calls"][-1]["history"])

    malformed = requests.post(
        upload, headers=headers, files={"file": ("broken.docx", b"not a ZIP document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}, timeout=30
    ).json()
    assert malformed["code"] == 0
    for stream, structured in ((True, False), (True, True), (False, False), (False, True)):
        for failed_ids, auth in (([uuid4().hex], headers), (ids, foreign_headers), ([malformed["data"]["id"]], headers)):
            before = len(state["calls"])
            response = request(failed_ids, stream, structured, auth)
            assert response.status_code == 400 and response.json()["detail"], response.text
            assert not response.headers["content-type"].startswith("text/event-stream")
            assert len(state["calls"]) == before
    env["storage"].remove_object(env["bucket"], f"{env['owners'][0]}-downloads/{ids[0]}")
    for stream, structured in ((True, False), (True, True), (False, False), (False, True)):
        before = len(state["calls"])
        response = request([ids[0]], stream, structured)
        assert response.status_code == 400 and len(state["calls"]) == before

    for failure in ("exception", "marker"):
        state["failure"] = failure
        for stream, structured in ((True, False), (True, True), (False, False), (False, True)):
            response = request([], stream, structured)
            if stream:
                assert response.status_code == 200
                frames = sse_frames(response.text)
                assert frames[-1]["retcode"] == 500 and not any(frame.get("data") is True for frame in frames)
            else:
                assert response.status_code == 500
    print(
        "real MCP HTTP: uploaded ID-only files, trusted registry/MinIO readback, real Canvas parsing, ordinary/structured/tools/no-tools and non-stream model inputs; missing/foreign/blob/parse failures rejected; model failures have no success completion. Model provider captured, no remote LLM or MCP tool invoked."
    )
