"""POST uses real HTTP/SQL/MinIO/Milvus with controlled embedding and faults."""

import base64
import sys
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import requests
from PIL import Image
from sqlalchemy.orm import Session

from api.db.db_models import Document
from tests.support.document_image_http import _save
from tests.support.document_image_http import bootstrapped_engine as bootstrapped_engine
from tests.support.document_image_http import image_http_api as image_http_api
from tests.support.document_image_http import image_http_database as image_http_database
from tests.support.document_image_read_service import _raw_object, _snapshot
from tests.support.document_image_read_service import image_resources as image_resources


def _image() -> bytes:
    output = BytesIO()
    Image.new("RGB", (3, 4), "blue").save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def creation_api(image_http_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    env = image_http_api
    document = uuid4().hex
    env["manifest"]["documents"]["creation"] = document
    env["register"]()
    with Session(env["engine"]) as db:
        db.add(Document(id=document, kb_id=env["ids"]["kb"], created_by=env["ids"]["owner"], name="image.txt", parser_id="naive", type="visual", chunk_num=0))
        db.commit()
    module = sys.modules["api.apps.restful_apis.chunk"]
    monkeypatch.setattr(module, "_embedding_model", lambda *_: SimpleNamespace(encode=lambda _: (np.ones((2, 768)), 0)))
    env.update(document=document, path=f"/api/v1/datasets/{env['ids']['kb']}/documents/{document}/chunks", module=module)
    return env


def _post(env: dict[str, Any], payload: dict[str, Any], role: str = "owner", path: str | None = None) -> dict[str, Any]:
    response = requests.post(env["base"] + (path or env["path"]), json=payload, headers={"Authorization": "Bearer " + env["tokens"][role]}, timeout=60)
    assert response.status_code == 200, response.content
    return response.json()


def test_creation_denials_have_zero_persistent_side_effects(creation_api: dict[str, Any]) -> None:
    env = creation_api
    before = _snapshot(env)
    health = requests.get(env["base"] + "/api/v1/dify/retrieval/health", timeout=30)
    assert health.status_code == 200 and health.json() == {"retcode": 0, "retmsg": "success", "data": True}
    assert requests.get(env["base"] + "/api/v1/dify/retrieval", timeout=30).status_code == 405
    assert _snapshot(env) == before
    for image in [None, "", " ", "%%%", "é", base64.b64encode(b"text").decode(), base64.b64encode(_image()[:-15]).decode()]:
        assert _post(env, {"content": "rejected", "image_base64": image})["code"] != 0
        assert _snapshot(env) == before
    valid = {"content": "rejected", "image_base64": base64.b64encode(_image()).decode()}
    assert _post(env, valid, "outsider")["code"] != 0
    assert _post(env, valid, path=env["path"].replace(env["document"], uuid4().hex))["code"] != 0
    assert _snapshot(env) == before
    _save(env["evidence"] / "creation-denials.json", {"before": before, "after": _snapshot(env)})


def test_creation_readable_and_same_id_retry_appends_without_duplicate_count(creation_api: dict[str, Any]) -> None:
    env = creation_api
    payload = {"content": "created", "image_base64": base64.b64encode(_image()).decode()}
    body = _post(env, payload)
    assert body["code"] == 0, body
    chunk = body["data"]["chunk"]
    key = chunk["id"]
    physical = env["adapter"]._resolve_bucket_and_path(env["ids"]["kb"], key)[1]
    assert _raw_object(env["client"], env["bucket"], physical) == _image()
    headers = {"Authorization": "Bearer " + env["tokens"]["owner"]}
    image = requests.get(env["base"] + "/api/v1/documents/images/" + chunk["image_id"], headers=headers, timeout=30)
    assert image.status_code == 200 and image.content == _image()
    for path in [env["path"], env["path"] + "/" + key]:
        response = requests.get(env["base"] + path, headers=headers, timeout=30)
        assert response.status_code == 200 and response.json()["code"] == 0, response.content
    again = _post(env, payload)
    assert again["code"] == 0 and again["data"]["chunk"]["id"] == key
    raw = _raw_object(env["client"], env["bucket"], physical)
    with Image.open(BytesIO(raw)) as combined:
        assert combined.size == (3, 8)
    after = _snapshot(env)
    rows = after["index"]["kb"]["rows"]
    assert len(rows) == 1 and rows[0]["img_id"] == chunk["image_id"]
    with Session(env["engine"]) as db:
        assert db.get(Document, env["document"]).chunk_num == 1
    _save(env["evidence"] / "creation-valid-retry.json", {"after": after, "same_id": key})


def test_create_storage_and_index_failures_report_actual_observations(creation_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = creation_api
    put, read, insert = env["storage"].put, env["storage"].get_bytes, env["module"].insert_source_chunks
    receipts = []
    for stage in ["read", "silent_put", "lost_put_reply", "confirm", "index", "lost_index_reply"]:
        before = _snapshot(env)
        written = False

        def faulty_put(*args: Any, **kwargs: Any) -> None:
            nonlocal written
            if stage == "silent_put":
                return
            put(*args, **kwargs)
            written = True
            if stage == "lost_put_reply":
                raise RuntimeError("controlled lost storage reply")

        def faulty_read(*args: Any, **kwargs: Any) -> bytes | None:
            if stage == "read" or (stage == "confirm" and written):
                raise RuntimeError("controlled unavailable read")
            return read(*args, **kwargs)

        def faulty_insert(*args: Any, **kwargs: Any) -> Any:
            if stage == "lost_index_reply":
                insert(*args, **kwargs)
            raise RuntimeError("controlled unavailable index reply")

        with monkeypatch.context() as fault:
            if stage in {"silent_put", "lost_put_reply", "confirm"}:
                fault.setattr(env["storage"], "put", faulty_put)
            if stage in {"read", "confirm"}:
                fault.setattr(env["storage"], "get_bytes", faulty_read)
            if "index" in stage:
                fault.setattr(env["module"], "insert_source_chunks", faulty_insert)
            body = _post(env, {"content": "failure-" + stage, "image_base64": base64.b64encode(_image()).decode()})
        assert body["code"] != 0 and body["data"]["retry_safe"] is False, body
        after = _snapshot(env)
        assert after["sql"] == before["sql"] and after["queue"] == before["queue"]
        expected_image = {"read": "not_attempted", "silent_put": "unchanged", "lost_put_reply": "changed", "confirm": "unknown", "index": "acknowledged", "lost_index_reply": "acknowledged"}[stage]
        assert body["data"]["image"] == expected_image
        if stage in {"read", "silent_put"}:
            assert after == before
        elif stage != "lost_index_reply":
            assert after["index"] == before["index"]
        else:
            assert len(after["index"]["kb"]["rows"]) == 1
        receipts.append({"stage": stage, "body": body, "before": before, "after": after})
    _save(env["evidence"] / "creation-failures.json", receipts)


@pytest.mark.external_consumer
def test_actual_web_create_serializer_and_transport(creation_api: dict[str, Any]) -> None:
    import json
    import os
    import shutil
    import subprocess
    from pathlib import Path

    env = creation_api
    checkout = Path(os.environ["WEB_COMPLETION_CHECKOUT"])
    script = Path(__file__).resolve().parents[1] / "support/chunk_image_web_consumer.mts"
    node = shutil.which("node")
    assert node is not None
    loader = checkout / "node_modules/tsx/dist/loader.mjs"
    env["journal"].event("borrow-web-consumer", checkout=str(checkout), script=str(script), runner=node, loader=str(loader))
    completed = subprocess.run(
        [node, "--import", str(loader), str(script)],
        cwd=checkout,
        env={**os.environ, "TSX_TSCONFIG_PATH": str(checkout / "tsconfig.app.json")},
        input=json.dumps({"base": env["base"], "token": env["tokens"]["owner"], "kb": env["ids"]["kb"], "doc": env["document"], "image": base64.b64encode(_image()).decode()}),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(completed.stdout.splitlines()[-1])
    assert receipt["passed"] is True
    after = _snapshot(env)
    assert len(after["index"]["kb"]["rows"]) == 2
    assert {row["content_with_weight"] for row in after["index"]["kb"]["rows"]} == {"actual-web-image", "actual-web-text"}
    with Session(env["engine"]) as db:
        assert db.get(Document, env["document"]).chunk_num == 2
    assert len(after["objects"]) == 1
    _save(env["evidence"] / "creation-actual-web.json", {"consumer": receipt, "after": after})
