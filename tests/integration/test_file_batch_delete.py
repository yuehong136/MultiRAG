"""Real JWT HTTP batch deletion with independent SQL and MinIO readback."""

from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, File, File2Document, Knowledgebase
from common import settings
from tests.support.document_image_http import _save
from tests.support.document_image_http import bootstrapped_engine as bootstrapped_engine
from tests.support.document_image_http import image_http_api as image_http_api
from tests.support.document_image_http import image_http_database as image_http_database
from tests.support.document_image_read_service import _snapshot
from tests.support.document_image_read_service import image_resources as image_resources


def test_batch_delete_http_readback(image_http_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, ids = image_http_api, image_http_api["ids"]
    names = ["root", "folder", "nested", "leaf", "blocked", "sibling", "denied", "linked", "doc", "relation"]
    owned = {name: uuid4().hex for name in names}
    file_names = names[:-2]
    env["manifest"]["file_ids"] = [owned[n] for n in file_names]
    env["register"]()
    with Session(env["engine"]) as db:
        for name in file_names:
            parent = "root" if name in {"root", "folder", "sibling", "denied", "linked"} else "folder" if name in {"nested", "blocked"} else "nested"
            folder = name in {"root", "folder", "nested"}
            db.add(
                File(
                    id=owned[name],
                    parent_id=owned[parent],
                    name=name,
                    tenant_id=ids["other"] if name == "denied" else ids["owner"],
                    created_by=ids["owner"],
                    type="folder" if folder else "doc",
                    location="" if folder else name,
                )
            )
            if not folder:
                env["storage"].put(owned[parent], name, name.encode())
        db.add(Document(id=owned["doc"], kb_id=ids["kb"], created_by=ids["owner"], name="linked", parser_id="naive", type="doc", location="linked"))
        db.add(File2Document(id=owned["relation"], file_id=owned["linked"], document_id=owned["doc"]))
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["kb"]).values(doc_num=1))
        db.commit()
    chunk_id = uuid4().hex
    assert (
        settings.docStoreConn.insert(
            [{"id": chunk_id, "pk": chunk_id, "doc_id": owned["doc"], "kb_id": ids["kb"], "content_with_weight": "delete me", "available_int": 1, "vector": [0.1] * 768, "q_768_vec": [0.1] * 768}],
            env["collections"]["kb"],
            ids["kb"],
        )
        == []
    )
    assert len(_snapshot(env)["index"]["kb"]["rows"]) == 1
    headers = {"Authorization": "Bearer " + env["tokens"]["owner"]}
    original_remove = env["client"].remove_object

    def fail_one(bucket: str, key: str, *args: Any, **kwargs: Any) -> Any:
        if key.endswith("/blocked"):
            raise OSError("injected object store failure")
        return original_remove(bucket, key, *args, **kwargs)

    monkeypatch.setattr(env["client"], "remove_object", fail_one)
    response = requests.delete(
        env["base"] + "/api/v1/files", headers=headers, json={"ids": ["missing", owned["folder"], owned["leaf"], owned["denied"], owned["sibling"], owned["sibling"], owned["linked"]]}, timeout=45
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 102 and body["data"]["success_count"] == 4, body
    with Session(env["engine"]) as db:
        remaining = set(db.scalars(sa.select(File.id).where(File.id.in_(env["manifest"]["file_ids"]))))
        assert remaining == {owned[n] for n in ["root", "folder", "blocked", "denied"]}
        assert db.get(Document, owned["doc"]) is None
        assert db.get(File2Document, owned["relation"]) is None
        assert db.get(Knowledgebase, ids["kb"]).doc_num == 0
    assert _snapshot(env)["index"]["kb"]["rows"] == []
    objects = {o.object_name for o in env["client"].list_objects(env["bucket"], recursive=True)}
    assert objects == {env["adapter"]._resolve_bucket_and_path(owned[parent], name)[1] for parent, name in [("folder", "blocked"), ("root", "denied")]}
    listing = requests.get(env["base"] + "/api/v1/files", headers=headers, params={"parent_id": owned["folder"]}, timeout=30).json()
    assert listing["code"] == 0 and {f["id"] for f in listing["data"]["files"]} == {owned["blocked"]}
    monkeypatch.setattr(env["client"], "remove_object", original_remove)
    retry = requests.delete(env["base"] + "/api/v1/files", headers=headers, json={"ids": [owned["folder"]]}, timeout=30).json()
    assert retry["code"] == 0 and retry["data"] == {"success_count": 2, "errors": []}
    with Session(env["engine"]) as db:
        assert db.get(File, owned["folder"]) is None and db.get(File, owned["blocked"]) is None
        assert db.get(File, owned["denied"]) is not None
    assert not settings.STORAGE_IMPL.obj_exist(owned["folder"], "blocked")
    _save(env["evidence"] / "file-batch-delete.readback.json", {"response": body, "remaining_files": sorted(remaining), "objects": sorted(objects), "list": listing, "retry": retry})
