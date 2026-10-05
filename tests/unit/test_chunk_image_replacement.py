"""REST image replacement preserves bytes while creation retains append semantics."""

import base64
import sys
from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from PIL import Image

from api.utils import image_utils
from common import settings
from common.constants import RetCode


def _image(color: str, size: tuple[int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class ImageStorage:
    def __init__(self) -> None:
        self.data = _image("red", (4, 5))
        self.writes: list[bytes] = []
        self.drop_write = False

    def obj_exist(self, bucket: str, name: str) -> bool:
        return True

    def get(self, bucket: str, name: str) -> bytes:
        return self.data

    def put(self, bucket: str, name: str, data: bytes) -> None:
        self.writes.append(data)
        if not self.drop_write:
            self.data = data


@pytest.fixture
def image_storage(monkeypatch: pytest.MonkeyPatch) -> ImageStorage:
    storage = ImageStorage()
    monkeypatch.setattr(settings, "STORAGE_IMPL", storage)
    monkeypatch.setattr(image_utils, "db_connection", lambda: nullcontext(SimpleNamespace(get_bind=lambda: None)))
    monkeypatch.setattr(image_utils, "image_write_locks", lambda *_args: nullcontext())
    return storage


def test_create_still_appends_and_replacement_is_exact(image_storage: ImageStorage) -> None:
    new = _image("blue", (2, 3))
    image_utils.store_chunk_image("kb", "chunk", new)
    with Image.open(BytesIO(image_storage.data)) as image:
        assert image.size == (4, 8)
        assert image.getpixel((0, 0))[0] > 200
        assert image.getpixel((0, 7))[2] > 200
    image_utils.replace_chunk_image("kb", "chunk", new)
    assert image_storage.data == new
    image_utils.replace_chunk_image("kb", "chunk", new)
    assert image_storage.data == new


def test_silent_storage_failure_is_not_success(image_storage: ImageStorage) -> None:
    old = image_storage.data
    image_storage.drop_write = True
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        image_utils.replace_chunk_image("kb", "chunk", _image("blue", (2, 3)))
    assert image_storage.data == old


def test_confirmed_append_detects_silent_storage_failure(image_storage: ImageStorage) -> None:
    old = image_storage.data
    image_storage.drop_write = True
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        image_utils.store_chunk_image("kb", "chunk", _image("blue", (2, 3)), verify=True)
    assert image_storage.data == old


def test_confirmed_append_uses_strict_bytes_despite_false_stat(image_storage: ImageStorage, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_storage, "obj_exist", lambda *_args: False)
    image_utils.store_chunk_image("kb", "chunk", _image("blue", (2, 3)), verify=True)
    with Image.open(BytesIO(image_storage.data)) as image:
        assert image.size == (4, 8)


def test_chunk_get_strips_native_vector(client: Any) -> None:
    module = sys.modules["api.apps.restful_apis.chunk"]
    payload = module._strip_chunk_runtime_fields({"img_id": "kb-c", "vector": np.ones(3, dtype=np.float32), "q_3_vec": [1, 2, 3], "create_timestamp_flt": np.float32(1234)})
    assert payload == {"img_id": "kb-c", "create_timestamp_flt": 1234.0}
    assert type(payload["create_timestamp_flt"]) is float


@pytest.fixture
def chunk_patch(client: Any, monkeypatch: pytest.MonkeyPatch, db: Any, image_storage: ImageStorage) -> tuple[Any, dict[str, Any]]:
    module = sys.modules["api.apps.restful_apis.chunk"]
    monkeypatch.setattr(db, "get_bind", lambda: None)
    row = {"id": "c1", "doc_id": "doc1", "img_id": "kb1-c1", "content_with_weight": "old", "available_int": 1}
    monkeypatch.setattr(module, "db_connection", lambda: nullcontext(db))
    monkeypatch.setattr(module, "image_write_locks", lambda *_args: nullcontext())
    monkeypatch.setattr(module, "_write_context", lambda *_args, **_kwargs: (SimpleNamespace(tenant_id="owner", name="kb"), SimpleNamespace(name="doc", parser_id="naive")))
    monkeypatch.setattr(module, "_embedding_model", lambda *_args: SimpleNamespace(db=None, encode=lambda _texts: (np.ones((2, 3)), 0)))
    monkeypatch.setattr(settings, "docStoreConn", SimpleNamespace(get=lambda *_args: dict(row), update=lambda _where, patch, *_args: row.update(patch) or True))
    return client, row


@pytest.mark.parametrize("payload", [{}, {"image_base64": None}, {"image_base64": ""}])
def test_patch_without_new_image_preserves_bytes(chunk_patch: tuple[Any, dict[str, Any]], image_storage: ImageStorage, payload: dict[str, Any]) -> None:
    client, row = chunk_patch
    old = image_storage.data
    response = client.patch("/api/v1/datasets/kb1/documents/doc1/chunks/c1", json=payload)
    assert response.status_code == 200 and response.json()["code"] == 0
    assert row["img_id"] == "kb1-c1" and image_storage.data == old and not image_storage.writes


@pytest.mark.parametrize("data", ["%%%", base64.b64encode(b"not an image").decode(), base64.b64encode(_image("blue", (2, 3))[:-15]).decode()])
def test_invalid_patch_image_has_no_mutation(chunk_patch: tuple[Any, dict[str, Any]], image_storage: ImageStorage, data: str) -> None:
    client, row = chunk_patch
    before = dict(row)
    response = client.patch("/api/v1/datasets/kb1/documents/doc1/chunks/c1", json={"content": "changed", "image_base64": data})
    assert response.status_code == 200 and response.json()["code"] == int(RetCode.DATA_ERROR)
    assert row == before and not image_storage.writes


@pytest.mark.parametrize("mode", [None, "append", "replace", " REPLACE "])
def test_patch_image_mode_contract(chunk_patch: tuple[Any, dict[str, Any]], image_storage: ImageStorage, mode: str | None) -> None:
    client, row = chunk_patch
    new = _image("blue", (2, 3))
    payload = {"image_base64": base64.b64encode(new).decode()}
    if mode is not None:
        payload["image_update_mode"] = mode
    response = client.patch("/api/v1/datasets/kb1/documents/doc1/chunks/c1", json=payload)
    assert response.status_code == 200 and response.json()["code"] == 0
    assert row["img_id"] == "kb1-c1"
    if mode and mode.strip().lower() == "replace":
        assert image_storage.data == new
    else:
        with Image.open(BytesIO(image_storage.data)) as image:
            assert image.size == (4, 8)


@pytest.mark.parametrize(
    "payload,status", [({"image_update_mode": "replace"}, 200), ({"image_update_mode": "append", "image_base64": ""}, 200), ({"image_update_mode": "remove"}, 422), ({"image_update_mode": "bad"}, 422)]
)
def test_invalid_mode_or_missing_image_has_no_mutation(chunk_patch: tuple[Any, dict[str, Any]], image_storage: ImageStorage, payload: dict[str, Any], status: int) -> None:
    client, row = chunk_patch
    before = dict(row)
    response = client.patch("/api/v1/datasets/kb1/documents/doc1/chunks/c1", json=payload)
    assert response.status_code == status
    if status == 200:
        assert response.json()["code"] == int(RetCode.DATA_ERROR)
    assert row == before and not image_storage.writes


@pytest.mark.parametrize("mode", [None, "replace"])
def test_patch_reports_partial_index_and_unchanged_image_on_silent_put(chunk_patch: tuple[Any, dict[str, Any]], image_storage: ImageStorage, mode: str | None) -> None:
    client, row = chunk_patch
    old = image_storage.data
    image_storage.drop_write = True
    payload = {"content": "new content", "image_base64": base64.b64encode(_image("blue", (2, 3))).decode()}
    if mode:
        payload["image_update_mode"] = mode
    response = client.patch("/api/v1/datasets/kb1/documents/doc1/chunks/c1", json=payload)
    assert response.status_code == 200 and response.json()["code"] == int(RetCode.EXCEPTION_ERROR)
    assert "索引更新已获确认" in response.json()["message"] and "不要盲目重发 append" in response.json()["message"]
    assert response.json()["data"] == {"outcome": "partial", "stage": "image_write", "index": "acknowledged", "image": "unchanged", "image_id": "kb1-c1", "retry_safe": False}
    assert row["content_with_weight"] == "new content" and image_storage.data == old
