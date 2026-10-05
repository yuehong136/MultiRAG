"""Real PATCH/JWT/protected reads, PG locks and independent MinIO/Milvus readback."""

import base64
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import requests
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.orm import Session

from api.db.db_models import Document
from common import settings
from tests.support.document_image_http import _save
from tests.support.document_image_http import bootstrapped_engine as bootstrapped_engine
from tests.support.document_image_http import image_http_api as image_http_api
from tests.support.document_image_http import image_http_database as image_http_database
from tests.support.document_image_read_service import _raw_object, _snapshot
from tests.support.document_image_read_service import image_resources as image_resources


def _image(color: str, size: tuple[int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def chunk_api(image_http_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    env = image_http_api
    doc, chunk = uuid4().hex, uuid4().hex
    kb = env["ids"]["kb"]
    env["manifest"]["documents"]["chunk"] = doc
    env["register"]()
    with Session(env["engine"]) as db:
        db.add(Document(id=doc, kb_id=kb, created_by=env["ids"]["owner"], name="image.txt", parser_id="naive", type="visual", chunk_num=1))
        db.commit()
    row = {"id": chunk, "pk": chunk, "doc_id": doc, "kb_id": kb, "img_id": f"{kb}-{chunk}", "doc_type_kwd": "image", "content_with_weight": "old", "q_768_vec": [0.1] * 768}
    assert settings.docStoreConn.insert([row], env["collections"]["kb"], kb) == []
    settings.docStoreConn._get_connection().flush([env["collections"]["kb"]], timeout=30)
    old = _image("red", (8, 9))
    env["storage"].put(kb, chunk, old)
    module = sys.modules["api.apps.restful_apis.chunk"]
    monkeypatch.setattr(module, "_embedding_model", lambda *_args: SimpleNamespace(db=None, encode=lambda _texts: (np.ones((2, 768)), 0)))
    env.update(chunk=chunk, doc=doc, old=old, path=f"/api/v1/datasets/{kb}/documents/{doc}/chunks/{chunk}", image_path=f"/api/v1/documents/images/{kb}-{chunk}")
    return env


def _patch(env: dict[str, Any], payload: dict[str, Any], role: str = "owner") -> dict[str, Any]:
    response = requests.patch(env["base"] + env["path"], json=payload, headers={"Authorization": "Bearer " + env["tokens"][role]}, timeout=40)
    assert response.status_code == 200, response.content
    return response.json()


def _readback(env: dict[str, Any], expected: bytes, content: str, mime: str = "image/png") -> dict[str, Any]:
    kb, chunk = env["ids"]["kb"], env["chunk"]
    physical = env["adapter"]._resolve_bucket_and_path(kb, chunk)[1]
    # Use an independent MinIO client read, not the helper's confirmation.
    assert _raw_object(env["client"], env["bucket"], physical) == expected
    snapshot = _snapshot(env)
    row = next(row for row in snapshot["index"]["kb"]["rows"] if row["id"] == chunk)
    assert row["img_id"] == f"{kb}-{chunk}" and row["content_with_weight"] == content
    response = requests.get(env["base"] + env["image_path"], headers={"Authorization": "Bearer " + env["tokens"]["owner"]}, timeout=30)
    assert response.status_code == 200, response.content
    assert response.content == expected and response.headers["content-type"] == mime
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    response = requests.get(env["base"] + env["path"], headers={"Authorization": "Bearer " + env["tokens"]["owner"]}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0
    assert response.json()["data"]["img_id"] == row["img_id"]
    return snapshot


def _payload(data: bytes, content: str = "new") -> dict[str, Any]:
    return {"content": content, "image_base64": base64.b64encode(data).decode(), "image_update_mode": "replace"}


def test_patch_replaces_exact_image_and_preserves_omitted_null_empty(chunk_api: dict[str, Any]) -> None:
    env = chunk_api
    new = _image("blue", (3, 4))
    before = _snapshot(env)
    denied = _patch(env, _payload(new), "outsider")
    assert denied["code"] != 0 and _snapshot(env) == before
    invalid = _patch(env, {"content": "invalid", "image_base64": base64.b64encode(b"invalid").decode()})
    assert invalid["code"] != 0 and _snapshot(env) == before
    for payload in [_payload(new), _payload(new), {}, {"image_base64": None}, {"image_base64": ""}]:
        assert _patch(env, payload)["code"] == 0
        after = _readback(env, new, "new")
        assert after["sql"] == before["sql"] and after["queue"] == before["queue"]
    for role in ["outsider", "other"]:
        response = requests.get(env["base"] + env["image_path"], headers={"Authorization": "Bearer " + env["tokens"][role]}, timeout=30)
        assert response.status_code == 404 and response.json()["code"] != 0
    _save(Path(env["evidence"]) / "chunk-replacement.json", {"before": before, "after": after, "image": new})
    smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, capture_output=True, text=True, timeout=60)
    (Path(env["evidence"]) / "chunk-smoke.log").write_text(smoke.stdout + smoke.stderr)
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr


def test_default_patch_keeps_append_compatibility(chunk_api: dict[str, Any]) -> None:
    env = chunk_api
    payload = _payload(_image("blue", (3, 4)))
    del payload["image_update_mode"]
    assert _patch(env, payload)["code"] == 0
    data = env["storage"].get_bytes(env["ids"]["kb"], env["chunk"])
    with Image.open(BytesIO(data)) as image:
        assert image.size == (8, 13) and image.format == "JPEG"
        assert image.getpixel((0, 0))[0] > 200 and image.getpixel((0, 12))[2] > 200
    _readback(env, data, "new", "image/jpeg")


@pytest.mark.parametrize("stage", ["index", "storage"])
def test_failed_patch_does_not_acknowledge_and_exposes_nonatomic_boundary(chunk_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    env = chunk_api
    new = _image("blue", (3, 4))
    before = _snapshot(env)
    with monkeypatch.context() as fault:
        if stage == "index":
            fault.setattr(settings.docStoreConn, "update", lambda *_args: False)
        else:
            # Production MinIO may return without raising after failed retries.
            fault.setattr(env["storage"], "put", lambda *_args: None)
        body = _patch(env, _payload(new))
    assert "code" in body or "retcode" in body
    assert body.get("code", body.get("retcode")) != 0
    after = _readback(env, env["old"], "old" if stage == "index" else "new")
    assert after["objects"] == before["objects"] and after["sql"] == before["sql"]
    if stage == "index":
        assert after["index"] == before["index"]
    _save(Path(env["evidence"]) / f"chunk-{stage}-failure.json", {"body": body, "before": before, "after": after, "atomic": False})


def test_concurrent_patch_serializes_and_last_acknowledged_image_is_exact(chunk_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = chunk_api
    first, second = _image("blue", (3, 4)), _image("green", (5, 6))
    ready, release = threading.Event(), threading.Event()
    original = env["storage"].put
    writes: list[bytes] = []

    def pause_put(bucket: str, key: str, data: bytes, *args: Any, **kwargs: Any) -> Any:
        writes.append(data)
        if key == env["chunk"] and data == first:
            ready.set()
            assert release.wait(20)
        return original(bucket, key, data, *args, **kwargs)

    monkeypatch.setattr(env["storage"], "put", pause_put)
    with ThreadPoolExecutor(max_workers=2) as pool:
        earlier = pool.submit(_patch, env, _payload(first, "first"))
        try:
            assert ready.wait(15), earlier.result(1) if earlier.done() else "PATCH has not reached object storage"
            later = pool.submit(_patch, env, _payload(second, "second"))
            deadline = time.monotonic() + 10
            while True:
                with env["engine"].connect() as db:
                    waiting = db.scalar(sa.text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%t_ai_documents%'"))
                if waiting:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert not later.done()
        finally:
            release.set()
        assert earlier.result(40)["code"] == 0
        second_body = later.result(40)
    if second_body["code"] == 0:
        assert writes == [first, second]
        winner, content = second, "second"
    else:
        # Milvus get uses Bounded visibility after update's delete+insert.
        # A rejected second request must not mutate bytes or claim success.
        assert second_body == {"code": 102, "message": f"Can't find this chunk {env['chunk']}"}
        assert writes == [first]
        winner, content = first, "first"
    after = _readback(env, winner, content)
    _save(Path(env["evidence"]) / "chunk-concurrent.json", {"waiting_document_lock": waiting, "second_response": second_body, "after": after, "winner": winner})
