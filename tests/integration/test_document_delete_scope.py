"""Dataset delete preflight: rejected HTTP requests cannot delete any state."""

from typing import Any
from urllib.parse import quote
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db import KNOWLEDGEBASE_FOLDER_NAME
from api.db.db_models import APIToken, Document, DocumentMetadata, File, File2Document, Knowledgebase, Task
from common import settings
from common.constants import FileSource
from core.utils.redis_conn import REDIS_CONN
from tests.support.document_image_http import _save
from tests.support.document_image_http import bootstrapped_engine as bootstrapped_engine
from tests.support.document_image_http import image_http_api as image_http_api
from tests.support.document_image_http import image_http_database as image_http_database
from tests.support.document_image_read_service import _snapshot
from tests.support.document_image_read_service import image_resources as image_resources


@pytest.mark.parametrize("entry", ["uuid-delete-all", "connector-web", "connector-api", "connector-legacy", "connector-legacy-single"])
def test_document_delete_scope_has_no_rejected_side_effects(image_http_api: dict[str, Any], entry: str) -> None:
    env, ids = image_http_api, image_http_api["ids"]
    documents = {kb: uuid4().hex if entry == "uuid-delete-all" else f"s3:{kb}:{uuid4().hex[:12]}" for kb in ["kb", "sibling", "foreign", "inactivekb"]}
    if entry != "connector-legacy-single" and entry != "uuid-delete-all":
        documents = {kb: doc_id.replace(":", "/", 1) for kb, doc_id in documents.items()}
    files = {kb: uuid4().hex for kb in documents}
    root, kb_folder, task_id = uuid4().hex, uuid4().hex, uuid4().hex
    env["manifest"].update(file_ids=[root, kb_folder, *files.values()], task_ids=[task_id])
    env["register"]()
    with Session(env["engine"]) as db:
        # Dataset creator and owning tenant need not be the same identity.
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["kb"]).values(created_by=ids["normal"]))
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["foreign"]).values(created_by=ids["owner"]))
        token = "document-delete-" + uuid4().hex
        db.add(APIToken(tenant_id=ids["owner"], token=token, name="delete-test"))
        db.add(File(id=root, parent_id=root, tenant_id=ids["owner"], created_by=ids["owner"], name="root", type="folder"))
        db.add(File(id=kb_folder, parent_id=root, tenant_id=ids["owner"], created_by=ids["owner"], name=KNOWLEDGEBASE_FOLDER_NAME, type="folder", source_type=FileSource.KNOWLEDGEBASE))
        for kb, doc_id in documents.items():
            owner = ids["other"] if kb == "foreign" else ids["owner"]
            db.add(Document(id=doc_id, kb_id=ids[kb], created_by=owner, name=kb, parser_id="naive", type="doc", location=kb, chunk_num=1, token_num=2))
            db.add(DocumentMetadata(id=doc_id, tenant_id=owner, kb_id=ids[kb], meta_fields={"kept": kb}))
            db.add(File(id=files[kb], parent_id=kb_folder, tenant_id=owner, created_by=owner, name=kb, type="doc", source_type=FileSource.KNOWLEDGEBASE, location=kb))
            db.add(File2Document(id=uuid4().hex, file_id=files[kb], document_id=doc_id))
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids[kb]).values(doc_num=1, chunk_num=1, token_num=2))
            env["storage"].put(ids[kb], kb, kb.encode())
            chunk_id = uuid4().hex
            assert (
                settings.docStoreConn.insert(
                    [{"id": chunk_id, "pk": chunk_id, "doc_id": doc_id, "kb_id": ids[kb], "content_with_weight": kb, "available_int": 1, "vector": [0.1] * 768, "q_768_vec": [0.1] * 768}],
                    env["collections"][kb],
                    ids[kb],
                )
                == []
            )
        db.add(Task(id=task_id, doc_id=documents["kb"], task_type="Parse", progress=0.25, digest="kept"))
        db.commit()
    assert REDIS_CONN.queue_product(env["queue"], {"id": task_id, "doc_id": documents["kb"]})
    headers = {"Authorization": "Bearer " + (token if entry == "connector-api" else env["tokens"]["owner"])}

    def delete(kb: str, payload: dict[str, Any]) -> requests.Response:
        if entry == "connector-legacy-single":
            return requests.delete(env["base"] + f"/v1/dataset/{ids[kb]}/documents/{quote(payload['ids'][0], safe='')}", headers=headers, timeout=45)
        if entry == "connector-legacy":
            return requests.post(env["base"] + "/v1/document/rm", headers=headers, json={"doc_id": payload["ids"]}, timeout=45)
        return requests.delete(env["base"] + f"/api/v1/datasets/{ids[kb]}/documents", headers=headers, json=payload, timeout=45)

    before = _snapshot(env)
    responses = []
    cases = [
        ("kb", {"ids": [documents["kb"], "missing"]}),
        ("kb", {"ids": ["missing", documents["kb"]]}),
        ("kb", {"ids": [documents["kb"], "\n!?。；！？\"'"]}),
        ("kb", {"ids": [documents["kb"], documents["sibling"]]}),
        ("kb", {"ids": [documents["foreign"], documents["kb"]]}),
        ("kb", {"ids": [documents["kb"], documents["foreign"]]}),
        ("inactivekb", {"ids": [documents["inactivekb"]]}),
        ("kb", {"ids": [documents["kb"]], "delete_all": True}),
        ("kb", {"ids": ["missing"], "delete_all": True}),
        ("foreign", {"delete_all": True}),
        ("kb", {}),
    ]
    if entry == "connector-legacy":
        cases = [(kb, payload) for kb, payload in cases if "ids" in payload and "delete_all" not in payload and documents["sibling"] not in payload["ids"]]
        cases.append(("foreign", {"ids": [documents["foreign"]]}))
    if entry == "connector-legacy-single":
        cases = [("kb", {"ids": [doc_id]}) for doc_id in ["missing", documents["sibling"], documents["foreign"]]]
        cases += [(kb, {"ids": [documents[kb]]}) for kb in ["foreign", "inactivekb"]]
    for kb, payload in cases:
        response = delete(kb, payload)
        if entry == "connector-legacy-single":
            assert response.status_code == (404 if kb == "kb" else 403), response.text
        else:
            assert response.status_code == 200, response.text
            assert response.json().get("retcode" if entry == "connector-legacy" else "code") == (109 if entry == "connector-legacy" else 102), response.text
        # Includes all SQL columns, relations, tasks, counters, metadata, raw object
        # bytes, complete Milvus payloads/vectors and the owned Redis queue.
        rejected_readback = _snapshot(env)
        assert rejected_readback == before, payload
        responses.append({"dataset": kb, "request": payload, "response": response.json(), "readback": rejected_readback})
    try:
        payload = {"delete_all": True} if entry == "uuid-delete-all" else {"ids": [documents["kb"], documents["kb"]]}
        response = delete("kb", payload)
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        assert response.json()["data"] == (True if entry.startswith("connector-legacy") else {"deleted": 1}), response.text
        after = _snapshot(env)
        with Session(env["engine"]) as db:
            assert db.get(Document, documents["kb"]) is None
            assert db.get(File, files["kb"]) is None
            assert db.get(Task, task_id) is None
            assert db.get(DocumentMetadata, documents["kb"]) is None
            assert db.get(Knowledgebase, ids["kb"]).doc_num == 0
            assert not list(db.scalars(sa.select(File2Document).where(File2Document.document_id == documents["kb"])))
            for kb in ["sibling", "foreign", "inactivekb"]:
                assert db.get(Document, documents[kb]) is not None
                assert db.get(File, files[kb]) is not None
                assert db.get(Knowledgebase, ids[kb]).doc_num == 1
        assert after["index"]["kb"]["rows"] == []
        for kb in ["sibling", "foreign", "inactivekb"]:
            assert after["index"][kb] == before["index"][kb]
        removed_key = env["adapter"]._resolve_bucket_and_path(ids["kb"], "kb")[1]
        assert after["objects"] == {key: value for key, value in before["objects"].items() if key != removed_key}
        assert REDIS_CONN.get(f"{task_id}-cancel")
        empty = requests.delete(env["base"] + f"/api/v1/datasets/{ids['kb']}/documents", headers=headers, json={"delete_all": True}, timeout=30).json()
        assert empty == {"code": 0, "data": {"deleted": 0}}
        _save(
            env["evidence"] / f"document-delete-scope.{entry}.readback.json",
            {"entry": entry, "rejected": responses, "before": before, "after": after, "deleted": response.json(), "empty_delete_all": empty},
        )
    finally:
        REDIS_CONN.REDIS.delete(f"{task_id}-cancel")
