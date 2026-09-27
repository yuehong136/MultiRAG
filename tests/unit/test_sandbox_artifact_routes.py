"""Sandbox artifact URLs and both download routes share the same contract."""

import base64
import importlib
import re
from types import SimpleNamespace
from typing import Any

import pytest

from api.apps.services import sandbox_artifact_service as artifacts
from api.utils.api_utils import async_current_user
from common.constants import SANDBOX_ARTIFACT_BUCKET, RetCode

_KEY = "a" * 32
_NEW_PATH = f"/api/v1/documents/artifact/{_KEY}"
_OLD_PATH = f"/v1/document/artifact/{_KEY}"
_RUN_ID = "b" * 32
_SESSION_ID = "c" * 32


class _Storage:
    def __init__(self, blob: bytes | None = b"binary\x00content") -> None:
        self.blob = blob
        self.reads: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str, bytes]] = []

    def get(self, bucket: str, name: str) -> bytes | None:
        self.reads.append((bucket, name))
        return self.blob

    def put(self, bucket: str, name: str, blob: bytes) -> None:
        self.writes.append((bucket, name, blob))

    def rm(self, bucket: str, name: str) -> None:
        self.reads.append((bucket, name))


@pytest.fixture
def storage(monkeypatch: Any) -> _Storage:
    storage = _Storage()
    monkeypatch.setattr(artifacts, "settings", SimpleNamespace(STORAGE_IMPL=storage))
    return storage


def test_code_exec_emits_rest_url_for_stored_bytes(monkeypatch: Any) -> None:
    code_exec = importlib.import_module("agent.tools.code_exec")
    storage = _Storage()
    monkeypatch.setattr(code_exec, "settings", SimpleNamespace(STORAGE_IMPL=storage))
    monkeypatch.setattr(code_exec.CodeExec, "_ensure_bucket_lifecycle", lambda _self: None)
    monkeypatch.setattr(code_exec, "record_artifact_binding", lambda *_args: True)

    tool = object.__new__(code_exec.CodeExec)
    tool._canvas = SimpleNamespace(task_id=_RUN_ID, artifact_owner_id="owner", artifact_session_id=_SESSION_ID)
    result = tool._upload_artifacts([{"name": "chart.PNG", "content_b64": base64.b64encode(b"image-bytes").decode(), "mime_type": "image/png", "size": 11}])

    assert len(result) == 1
    assert re.fullmatch(rf"/api/v1/documents/artifact/[0-9a-f]{{32}}\.png\?run_id={_RUN_ID}&session_id={_SESSION_ID}", result[0]["url"])
    filename = result[0]["url"].split("/", 5)[-1].split("?", 1)[0]
    assert storage.writes == [(SANDBOX_ARTIFACT_BUCKET, filename, b"image-bytes")]


@pytest.mark.parametrize("path", [_NEW_PATH, _OLD_PATH])
def test_authorized_routes_return_exact_bytes_and_safe_headers(client: Any, monkeypatch: Any, storage: _Storage, path: str) -> None:
    async def authorized(*_args: Any) -> bool:
        return True

    monkeypatch.setattr(artifacts, "_artifact_accessible", authorized)
    response = client.get(f"{path}.pdf")

    assert response.status_code == 200
    assert response.content == storage.blob
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"] == f'inline; filename="{_KEY}.pdf"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    assert storage.reads == [(SANDBOX_ARTIFACT_BUCKET, f"{_KEY}.pdf")]


@pytest.mark.parametrize("path", [_NEW_PATH, _OLD_PATH])
@pytest.mark.parametrize("extension,content_type", [("html", "text/html"), ("svg", "image/svg+xml")])
def test_active_content_is_download_only(client: Any, monkeypatch: Any, storage: _Storage, path: str, extension: str, content_type: str) -> None:
    async def authorized(*_args: Any) -> bool:
        return True

    monkeypatch.setattr(artifacts, "_artifact_accessible", authorized)
    response = client.get(f"{path}.{extension}")

    assert response.content == storage.blob
    assert response.headers["content-type"].startswith(content_type)
    assert response.headers["content-disposition"] == f'attachment; filename="{_KEY}.{extension}"'
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", [_NEW_PATH, _OLD_PATH])
def test_invalid_names_and_denied_artifacts_never_read_storage(client: Any, monkeypatch: Any, storage: _Storage, path: str) -> None:
    async def denied(*_args: Any) -> bool:
        return False

    monkeypatch.setattr(artifacts, "_artifact_accessible", denied)
    for suffix, message in [("bad.pdf", "Invalid filename."), (f"{_KEY}.exe", "Invalid file type."), (f"{_KEY}.pdf%5Cother", "Invalid filename.")]:
        response = client.get(f"{path.rsplit('/', 1)[0]}/{suffix}")
        assert response.status_code == 200
        assert response.json()["retcode"] == int(RetCode.DATA_ERROR)
        assert response.json()["retmsg"] == message

    traversal = client.get(f"{path.rsplit('/', 1)[0]}/../report.pdf")
    assert traversal.status_code in {200, 404}
    if traversal.status_code == 200:
        assert traversal.json().get("retcode", traversal.json().get("code")) != 0

    denied_response = client.get(f"{path}.pdf")
    assert denied_response.json()["retcode"] == int(RetCode.DATA_ERROR)
    assert denied_response.json()["retmsg"] == "Artifact not found."
    assert storage.reads == []


@pytest.mark.parametrize("path", [_NEW_PATH, _OLD_PATH])
def test_missing_blob_is_business_error(client: Any, monkeypatch: Any, storage: _Storage, path: str) -> None:
    async def authorized(*_args: Any) -> bool:
        return True

    monkeypatch.setattr(artifacts, "_artifact_accessible", authorized)
    storage.blob = None
    response = client.get(f"{path}.pdf")
    assert response.json()["retcode"] == int(RetCode.DATA_ERROR)
    assert response.json()["retmsg"] == "Artifact not found."


def test_both_routes_require_authentication(client: Any, storage: _Storage) -> None:
    client.app.dependency_overrides.pop(async_current_user)
    for path in (_NEW_PATH, _OLD_PATH):
        response = client.get(f"{path}.pdf")
        assert response.status_code in {401, 403}
    assert storage.reads == []


def test_openapi_marks_only_legacy_route_deprecated(client: Any) -> None:
    paths = client.app.openapi()["paths"]
    assert paths["/v1/document/artifact/{filename}"]["get"]["deprecated"] is True
    assert paths["/api/v1/documents/artifact/{filename}"]["get"].get("deprecated") is None


def test_artifact_url_is_not_emitted_if_binding_fails(monkeypatch: Any) -> None:
    code_exec = importlib.import_module("agent.tools.code_exec")
    storage = _Storage()
    monkeypatch.setattr(code_exec, "settings", SimpleNamespace(STORAGE_IMPL=storage))
    monkeypatch.setattr(code_exec.CodeExec, "_ensure_bucket_lifecycle", lambda _self: None)
    monkeypatch.setattr(code_exec, "record_artifact_binding", lambda *_args: False)
    tool = object.__new__(code_exec.CodeExec)
    tool._canvas = SimpleNamespace(task_id=_RUN_ID, artifact_owner_id="owner", artifact_session_id=_SESSION_ID)

    assert tool._upload_artifacts([{"name": "chart.png", "content_b64": base64.b64encode(b"image").decode(), "mime_type": "image/png"}]) == []
    assert len(storage.writes) == 1
    assert storage.reads == [(SANDBOX_ARTIFACT_BUCKET, storage.writes[0][1])]
