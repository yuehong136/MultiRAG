"""Real authenticated search, PostgreSQL and Milvus parent/child readback."""

import json
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document
from common import settings
from tests.integration.test_dataset_search_http import request
from tests.integration.test_dataset_search_http import search_api as search_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


def test_http_parent_fallback_and_second_dataset_parent(search_api: dict[str, Any]) -> None:
    env = search_api
    parent_id = uuid4().hex
    store = settings.docStoreConn
    for key in ("dataset", "second"):
        row = {**env["rows"][key][1], "mom_id": parent_id}
        assert store.insert([row], env["collections"][key], env[key]) == []
    parent = {**env["rows"]["second"][1], "id": parent_id, "mom_id": "", "content_with_weight": "second dataset complete parent", "available_int": 0}
    assert store.insert([parent], env["collections"]["second"], env["second"]) == []
    for key in ("dataset", "second"):
        env["reader"].flush(env["collections"][key], timeout=60)
    raw = env["reader"].query(env["collections"]["second"], filter="pk == " + json.dumps(parent_id), output_fields=["pk", "available_int", "content_with_weight"], consistency_level="Strong")
    assert raw[0]["available_int"] == 0 and raw[0]["content_with_weight"] == "second dataset complete parent"
    payload = {"question": "availability", "dataset_ids": [env["dataset"], env["second"]], "search_mode": {"type": "dense"}, "similarity_threshold": 0}

    def retrieve() -> list[dict[str, Any]]:
        response = request(env, "POST", "/search", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["code"] == 0, body
        return body["data"]["chunks"]

    chunks = retrieve()
    assert {chunk["chunk_id"] for chunk in chunks} == {env["rows"]["dataset"][1]["id"], parent_id}
    assert next(chunk for chunk in chunks if chunk["chunk_id"] == parent_id)["text"] == "second dataset complete parent"
    with Session(env["engine"]) as writer:
        writer.execute(sa.update(Document).where(Document.id == env["second_doc"]).values(status="0"))
        writer.commit()
    with Session(env["engine"]) as reader:
        assert reader.get(Document, env["second_doc"]).status == "0"
    assert {chunk["chunk_id"] for chunk in retrieve()} == {env["rows"]["dataset"][1]["id"]}
    with Session(env["engine"]) as writer:
        writer.execute(sa.delete(Document).where(Document.id == env["doc"]))
        writer.commit()
    for _ in range(2):
        assert retrieve() == []
