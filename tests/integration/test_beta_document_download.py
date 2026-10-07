"""Registered beta download HTTP with real JWT/token SQL and owned MinIO bytes.

Source-file linking shares content into a dataset; no auth/storage mocks.
Only request-independent resource namespaces are redirected to owned scratch.
"""

import copy
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import APIToken, Document, File, File2Document, Knowledgebase, Tenant, UserTenant
from common import resources, settings
from core.utils.encrypted_storage import EncryptedStorageWrapper
from tests.support.agent_update_release import release_api as release_api


@pytest.fixture
def downloads(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = release_api
    owner, member, outsider = env["owners"]
    dataset = {key: uuid4().hex for key in ["private", "team", "foreign", "inactive"]}
    docs = {key: uuid4().hex for key in ["private", "team", "foreign", "inactive", "deleted", "orphan", "local", "shared", "empty"]}
    folders = {key: uuid4().hex for key in ["owner", "foreign"]}
    file_ids = [uuid4().hex, uuid4().hex]
    file_tenants = [owner, outsider]
    beta = {key: f"download-test-{uuid4().hex}" for key in ["owner", "member", "outsider", "ambiguous", "orphan"]}
    token_ids: list[str] = []
    bucket = f"beta-download-{uuid4().hex}"
    source = settings.STORAGE_IMPL
    if isinstance(source, EncryptedStorageWrapper):
        storage = copy.copy(source)
        adapter = copy.copy(source.storage_impl)
        storage.storage_impl = adapter
    else:
        storage = adapter = copy.copy(source)
    adapter.bucket, adapter.prefix_path = bucket, "download-test"
    client = adapter.conn
    reads: list[tuple[str, str]] = []
    get = storage.get

    def observed_get(namespace: str, key: str) -> Any:
        reads.append((namespace, key))
        return get(namespace, key)

    monkeypatch.setattr(storage, "get", observed_get)
    monkeypatch.setitem(resources._state, "storage", storage)
    with ExitStack() as cleanup:

        def remove_bucket() -> None:
            if client.bucket_exists(bucket):
                for item in client.list_objects(bucket, recursive=True):
                    client.remove_object(bucket, item.object_name)
                client.remove_bucket(bucket)
            assert not client.bucket_exists(bucket)

        def remove_rows() -> None:
            with Session(env["engine"]) as db:
                conditions = [
                    (File2Document, File2Document.document_id.in_(list(docs.values()))),
                    (File, File.id.in_([*file_ids, *folders.values()])),
                    (Document, Document.id.in_(list(docs.values()))),
                    (Knowledgebase, Knowledgebase.id.in_(list(dataset.values()))),
                    (APIToken, APIToken.token.in_(token_ids)),
                ]
                for model, condition in conditions:
                    db.execute(sa.delete(model).where(condition))
                db.commit()
            with env["engine"].connect() as db:
                for model, condition in conditions:
                    assert db.scalar(sa.select(sa.func.count()).select_from(model).where(condition)) == 0

        cleanup.callback(remove_bucket)
        cleanup.callback(remove_rows)
        with Session(env["engine"]) as db:
            db.add(UserTenant(id=uuid4().hex, user_id=member, tenant_id=owner, role="normal", status="1", invited_by=owner))
            for key, kb_id in dataset.items():
                db.add(
                    Knowledgebase(
                        id=kb_id,
                        tenant_id=outsider if key == "foreign" else owner,
                        name=key,
                        embd_id="",
                        created_by=owner,
                        permission="team" if key in {"team", "foreign"} else "me",
                        status="0" if key == "inactive" else "1",
                    )
                )
            for key, doc_id in docs.items():
                kb_id = dataset.get(key, dataset["private"])
                if key == "orphan":
                    kb_id = uuid4().hex
                db.add(Document(id=doc_id, kb_id=kb_id, name=f"{key} 空间.txt", location=f"nested/{key}.txt", parser_id="naive", type="doc", created_by=owner, status="0" if key == "deleted" else "1"))
            for label, tenant in [("owner", owner), ("foreign", outsider)]:
                db.add(File(id=folders[label], parent_id=folders[label], tenant_id=tenant, created_by=tenant, name="folder", type="folder"))
            for key, fid, tenant in zip(["local", "shared"], file_ids, file_tenants, strict=True):
                parent = folders["owner" if key == "local" else "foreign"]
                db.add(File(id=fid, parent_id=parent, tenant_id=tenant, created_by=tenant, name=key, location=f"nested/{key}.txt", type="doc"))
                db.add(File2Document(id=uuid4().hex, file_id=fid, document_id=docs[key]))
            for key, tenant in [("owner", owner), ("member", member), ("outsider", outsider), ("orphan", uuid4().hex), ("ambiguous", owner), ("ambiguous", outsider)]:
                tid = f"normal-{uuid4().hex}"
                token_ids.append(tid)
                db.add(APIToken(tenant_id=tenant, token=tid, beta=beta[key], name="download-test"))
            db.commit()
        assert not client.bucket_exists(bucket)
        client.make_bucket(bucket)
        for key in ["private", "team", "foreign", "inactive", "deleted", "empty"]:
            kb_id = dataset.get(key, dataset["private"])
            storage.put(kb_id, f"nested/{key}.txt", b"" if key == "empty" else key.encode())
        storage.put(folders["owner"], "nested/local.txt", b"local-source")
        storage.put(folders["foreign"], "nested/shared.txt", b"shared-source")
        yield dict(env, beta=beta, datasets=dataset, docs=docs, folders=folders, file_ids=file_ids, reads=reads, storage=storage, bucket=bucket, object_client=client)


def download(env: dict[str, Any], key: str, *, token: str = "owner", header: str | None = None, dataset: str | None = None) -> Any:
    path = f"/api/v1/documents/{env['docs'][key]}" if dataset is None else f"/api/v1/datasets/{env['datasets'][dataset]}/documents/{env['docs'][key]}"
    authorization = header if header is not None else f"Bearer {env['beta'][token]}"
    return env["client"].get(env["base"] + path, headers={"Authorization": authorization}, timeout=30)


def snapshot(env: dict[str, Any]) -> Any:
    sql = []
    with Session(env["engine"]) as db:
        for model, condition in [
            (Knowledgebase, Knowledgebase.id.in_(list(env["datasets"].values()))),
            (Document, Document.id.in_(list(env["docs"].values()))),
            (File, File.id.in_([*env["file_ids"], *env["folders"].values()])),
            (File2Document, File2Document.document_id.in_(list(env["docs"].values()))),
        ]:
            sql.append([dict(row._mapping) for row in db.execute(sa.select(model.__table__).where(condition).order_by(model.__table__.c.id))])
    objects = {}
    for item in env["object_client"].list_objects(env["bucket"], recursive=True):
        response = env["object_client"].get_object(env["bucket"], item.object_name)
        try:
            objects[item.object_name] = response.read()
        finally:
            response.close()
            response.release_conn()
    return sql, objects


def test_registered_routes_and_real_same_tenant_downloads(downloads: dict[str, Any]) -> None:
    from api import apps

    env = downloads
    from fastapi.routing import iter_route_contexts

    for path, function in [("/api/v1/documents/{document_id}", "download_doc"), ("/api/v1/datasets/{dataset_id}/documents/{document_id}", "download_document")]:
        context = next(ctx for ctx in iter_route_contexts(apps.app.routes) if ctx.path == path and "GET" in (ctx.methods or set()))
        assert context.original_route.endpoint.__module__ == "api.apps.sdk.doc" and context.original_route.endpoint.__name__ == function
    before = snapshot(env)
    for key, content in [("private", b"private"), ("team", b"team"), ("local", b"local-source"), ("shared", b"shared-source")]:
        response = download(env, key)
        assert response.status_code == 200 and response.content == content
        assert response.headers["Content-Type"] == "application/octet-stream"
        assert "filename*=UTF-8''" in response.headers["Content-Disposition"]
    response = download(env, "private", header=f"Bearer {env['keys'][0]}", dataset="private")
    assert response.status_code == 200 and response.content == b"private"
    response = download(env, "empty")
    assert response.json()["code"] != 0 and "empty" in response.json()["message"]
    assert snapshot(env) == before and len(env["reads"]) == 6


def test_rejected_tokens_and_tenants_never_reach_storage(downloads: dict[str, Any]) -> None:
    env = downloads
    before = snapshot(env)
    for header in ["", "Bearer", "Basic invalid", "Bearer invalid extra", "Bearer invalid", f"Basic {env['beta']['owner']}"]:
        response = download(env, "private", header=header)
        assert response.status_code == 200 and response.json()["code"] != 0
    for key, token in [
        ("private", "ambiguous"),
        ("private", "orphan"),
        ("private", "member"),
        ("team", "member"),
        ("foreign", "owner"),
        ("deleted", "owner"),
        ("inactive", "owner"),
        ("orphan", "owner"),
    ]:
        response = download(env, key, token=token)
        assert response.status_code == 200 and response.json()["code"] != 0
    with Session(env["engine"]) as db:
        db.execute(sa.update(Tenant).where(Tenant.id == env["owners"][0]).values(status="0"))
        db.commit()
    assert download(env, "private").json()["code"] != 0
    assert not env["reads"] and snapshot(env) == before


def test_storage_address_provenance_and_dataset_binding(downloads: dict[str, Any]) -> None:
    env = downloads
    before = snapshot(env)
    for key, dataset in [("foreign", "private"), ("private", "team"), ("private", "foreign")]:
        response = download(env, key, header=f"Bearer {env['keys'][0]}", dataset=dataset)
        assert response.json()["code"] != 0
    assert snapshot(env) == before
    fid = env["file_ids"][0]
    mutations = [
        (File, File.id == fid, {"parent_id": env["folders"]["foreign"]}),
        (File, File.id == fid, {"parent_id": uuid4().hex}),
        (File, File.id == fid, {"location": "../private.txt"}),
        (File, File.id == fid, {"location": "/absolute"}),
        (File, File.id == fid, {"location": "nested\\escape"}),
        (Document, Document.id == env["docs"]["private"], {"location": "../foreign.txt"}),
    ]
    for model, condition, values in mutations:
        with Session(env["engine"]) as db:
            row = db.scalar(sa.select(model).where(condition))
            original = {key: getattr(row, key) for key in values}
            db.execute(sa.update(model).where(condition).values(**values))
            db.commit()
        current = snapshot(env)
        key = "local" if model is File else "private"
        assert download(env, key).json()["code"] != 0
        assert not env["reads"] and snapshot(env) == current
        with Session(env["engine"]) as db:
            db.execute(sa.update(model).where(condition).values(**original))
            db.commit()
    with Session(env["engine"]) as db:
        db.add(File2Document(id=uuid4().hex, document_id=env["docs"]["local"], file_id=env["file_ids"][1]))
        db.commit()
    current = snapshot(env)
    assert download(env, "local").json()["code"] != 0
    assert not env["reads"] and snapshot(env) == current
