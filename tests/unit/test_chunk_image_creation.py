"""POST validates before work and confirms object bytes before native insertion."""

import base64
import sys
from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import numpy as np
import pytest
from PIL import Image

from api.utils import image_utils
from common import settings


def _image() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 3), "blue").save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def creation(client: Any, db: Any, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    module = sys.modules["api.apps.restful_apis.chunk"]
    events: list[str] = []
    storage: dict[str, bytes] = {}
    rows: dict[str, Any] = {}
    monkeypatch.setattr(db, "get_bind", lambda: None)
    monkeypatch.setattr(db, "connection", lambda: None)
    monkeypatch.setattr(module, "db_connection", lambda: nullcontext(db))
    monkeypatch.setattr(image_utils, "db_connection", lambda: nullcontext(db))
    monkeypatch.setattr(module, "image_write_locks", lambda *_args: nullcontext())
    monkeypatch.setattr(image_utils, "image_write_locks", lambda *_args: nullcontext())
    context = Mock(return_value=(SimpleNamespace(tenant_id="owner", name="kb"), SimpleNamespace(name="doc")))
    monkeypatch.setattr(module, "_write_context", context)
    model = Mock(return_value=SimpleNamespace(encode=lambda _: (np.ones((2, 3)), 0)))
    monkeypatch.setattr(module, "_embedding_model", model)

    def put(_bucket: str, name: str, data: bytes) -> None:
        events.append("put")
        storage[name] = data

    adapter = SimpleNamespace(get_bytes=lambda _bucket, name: storage.get(name), put=put)
    monkeypatch.setattr(settings, "STORAGE_IMPL", adapter)

    def insert(_bind: Any, chunks: list[dict[str, Any]], *_args: Any) -> list[str]:
        events.append("index")
        rows.update({chunk["id"]: dict(chunk) for chunk in chunks})
        return []

    monkeypatch.setattr(module, "insert_source_chunks", insert)
    reconcile, increment = Mock(), Mock()
    monkeypatch.setattr(module, "reconcile_task_chunk_count", reconcile)
    monkeypatch.setattr(module.DocumentService, "increment_chunk_num", increment)
    return SimpleNamespace(
        client=client,
        module=module,
        events=events,
        storage=storage,
        rows=rows,
        adapter=adapter,
        model=model,
        context=context,
        reconcile=reconcile,
        increment=increment,
        path="/api/v1/datasets/kb1/documents/doc1/chunks",
    )


@pytest.mark.parametrize("value", [None, "", " ", "%%%", "é", "YQ==\n", base64.b64encode(b"text").decode(), base64.b64encode(_image()[:-15]).decode(), 42, [], {}])
def test_supplied_invalid_image_has_no_side_effect(creation: SimpleNamespace, value: Any) -> None:
    env = creation
    response = env.client.post(env.path, json={"content": "content", "image_base64": value})
    assert response.status_code in {200, 422}
    if response.status_code == 200:
        assert response.json()["code"] != 0
    assert not env.events and not env.rows and not env.storage
    env.model.assert_not_called()
    env.reconcile.assert_not_called()
    env.increment.assert_not_called()


def test_valid_image_confirmed_before_index_and_same_id_appends(creation: SimpleNamespace) -> None:
    env = creation
    payload = {"content": "content", "image_base64": base64.b64encode(_image()).decode()}
    first = env.client.post(env.path, json=payload).json()
    assert first["code"] == 0 and env.events == ["put", "index"]
    chunk = first["data"]["chunk"]
    assert chunk["image_id"] == "kb1-" + chunk["id"] and env.storage[chunk["id"]] == _image()
    assert env.client.post(env.path, json=payload).json()["code"] == 0
    assert len(env.rows) == 1 and env.events == ["put", "index", "put", "index"]
    with Image.open(BytesIO(env.storage[chunk["id"]])) as image:
        assert image.size == (2, 6)


def test_omitted_image_remains_text_only(creation: SimpleNamespace) -> None:
    env = creation
    response = env.client.post(env.path, json={"content": "content"})
    assert response.json()["code"] == 0 and env.events == ["index"]
    assert response.json()["data"]["chunk"]["image_id"] == ""


@pytest.mark.parametrize("fault", ["silent_put", "raising_put", "lost_put_reply", "read", "confirm", "index", "lost_index_reply"])
def test_failed_create_reports_observed_boundaries(creation: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    env = creation
    put, read, insert = env.adapter.put, env.adapter.get_bytes, env.module.insert_source_chunks

    def failed_put(*args: Any) -> None:
        if fault in {"lost_put_reply", "confirm"}:
            put(*args)
        if fault != "silent_put":
            raise RuntimeError("lost put reply")

    def failed_read(*args: Any) -> bytes | None:
        if fault == "read" or (fault == "confirm" and env.storage):
            raise RuntimeError("unavailable read")
        return read(*args)

    def failed_insert(*args: Any) -> None:
        if fault == "lost_index_reply":
            insert(*args)
        raise RuntimeError("unavailable index")

    if "put" in fault or fault == "confirm":
        monkeypatch.setattr(env.adapter, "put", failed_put)
    if fault in {"read", "confirm"}:
        monkeypatch.setattr(env.adapter, "get_bytes", failed_read)
    if "index" in fault:
        monkeypatch.setattr(env.module, "insert_source_chunks", failed_insert)
    body = env.client.post(env.path, json={"content": "content", "image_base64": base64.b64encode(_image()).decode()}).json()
    assert body["code"] != 0 and body["data"]["retry_safe"] is False
    state = body["data"]
    if "index" in fault:
        assert state["index"] == "unknown" and state["image"] == "acknowledged" and env.storage
        assert bool(env.rows) == (fault == "lost_index_reply")
    else:
        assert state["index"] == "not_attempted" and not env.rows
        assert state["image"] == {"silent_put": "unchanged", "raising_put": "unchanged", "lost_put_reply": "changed", "read": "not_attempted", "confirm": "unknown"}[fault]
    env.reconcile.assert_not_called()
    env.increment.assert_not_called()
