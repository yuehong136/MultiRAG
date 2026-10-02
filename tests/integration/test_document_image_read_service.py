"""Real service-entry reads: isolated SQL, MinIO, Milvus and Redis, no HTTP.

Snapshot every relevant SQL column, full index payload/vectors/schema/count,
physical object bytes and owned Redis stream before/after the read matrix.
"""

import base64
import copy
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import sqlalchemy as sa
from minio import Minio
from PIL import Image
from pymilvus import MilvusClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from api.db.db_models import APIToken, Document, DocumentMetadata, File, File2Document, Knowledgebase, Task, Tenant, User, UserTenant
from api.db.services import document_image_service as service
from api.db.services.file_service import FileService
from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, Principal, TenantMembershipEvidence, build_principal_from_authenticated_actor
from common import resources, settings
from common.bootstrap import ensure_initialized
from common.config_utils import CONFIGS
from core.nlp.search import index_name_one
from core.utils.encrypted_storage import EncryptedStorageWrapper
from core.utils.redis_conn import REDIS_CONN


def _principal(user_id: str, source: AuthenticationSource = AuthenticationSource.WEB_SESSION) -> Principal:
    return build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(platform_user_id=user_id),
        membership=TenantMembershipEvidence(platform_user_id=user_id, tenant_id=user_id),
        authentication=AuthenticationContext(source=source, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
    )


def _image(fmt: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (3, 3), "blue").save(buffer, format=fmt)
    return buffer.getvalue()


def _json(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_base64": base64.b64encode(value).decode()}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(type(value).__name__)


def _raw_object(client: Minio, bucket: str, key: str) -> bytes:
    response = client.get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


@pytest.fixture
def image_resources(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    ensure_initialized()
    ids = {name: uuid4().hex for name in ["owner", "other", "normal", "admin", "invite", "inactive", "outsider", "kb", "sibling", "foreign", "inactivekb"]}
    names = {name: f"image_read_{uuid4().hex}" for name in ["kb", "sibling", "foreign", "inactivekb"]}
    bucket, queue = f"image-read-{uuid4().hex}", f"image-read:{uuid4().hex}:queue"
    collections = {name: index_name_one(ids["other"] if name == "foreign" else ids["owner"], names[name]) for name in names}
    evidence = Path(os.environ.get("MULTIRAG_C511_IMAGE_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence / f"{ids['kb']}.manifest.json"
    manifest = {
        "database": bootstrapped_engine.url.database,
        "ids": ids,
        "collections": collections,
        "bucket": bucket,
        "prefix": "image-read",
        "queue": queue,
        "listener": None,
        "pid": None,
        "cleanup": False,
    }
    manifest_path.write_text(json.dumps(manifest))
    cfg = CONFIGS["minio"]
    client = Minio(cfg["host"], access_key=cfg["user"], secret_key=cfg["password"], secure=str(cfg.get("secure", False)).lower() in {"true", "1", "yes"})
    source = settings.STORAGE_IMPL
    if isinstance(source, EncryptedStorageWrapper):
        storage = copy.copy(source)
        adapter = copy.copy(source.storage_impl)
        storage.storage_impl = adapter
    else:
        storage = adapter = copy.copy(source)
    adapter.conn, adapter.bucket, adapter.prefix_path = client, bucket, "image-read"
    monkeypatch.setitem(resources._state, "storage", storage)
    store = settings.docStoreConn
    assert store.db_type() == "milvus"
    assert not client.bucket_exists(bucket) and not REDIS_CONN.REDIS.exists(queue)
    assert all(not store.has_collection(collection) for collection in collections.values())
    client.make_bucket(bucket)
    env = {
        "engine": bootstrapped_engine,
        "ids": ids,
        "names": names,
        "collections": collections,
        "bucket": bucket,
        "queue": queue,
        "client": client,
        "storage": storage,
        "adapter": adapter,
        "evidence": evidence,
        "manifest": manifest,
        "manifest_path": manifest_path,
    }
    try:
        with Session(bootstrapped_engine) as db:
            for role in ["owner", "other", "normal", "admin", "invite", "inactive", "outsider"]:
                db.add(User(id=ids[role], email=f"{ids[role]}@image.test", nickname=role, password="unused", access_token="active"))
            for role in ["owner", "other"]:
                db.add(Tenant(id=ids[role], name="Image read scratch", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
                db.add(UserTenant(id=uuid4().hex, user_id=ids[role], tenant_id=ids[role], role="owner", invited_by=ids[role]))
            for role in ["normal", "admin", "invite", "inactive"]:
                db.add(
                    UserTenant(
                        id=uuid4().hex, user_id=ids[role], tenant_id=ids["owner"], role="normal" if role == "inactive" else role, status="0" if role == "inactive" else "1", invited_by=ids["owner"]
                    )
                )
            for kb in names:
                owner = ids["other"] if kb == "foreign" else ids["owner"]
                db.add(Knowledgebase(id=ids[kb], name=names[kb], tenant_id=owner, created_by=owner, embd_id="scratch", parser_id="naive", parser_config={}, status="0" if kb == "inactivekb" else "1"))
            db.commit()
        yield env
    finally:
        # Capture complete final physical state before removing only owned resources.
        (evidence / f"{ids['kb']}.before-cleanup.json").write_text(json.dumps(_snapshot(env), default=_json, indent=2))
        manifest["objects"] = [obj.object_name for obj in client.list_objects(bucket, recursive=True)]
        manifest_path.write_text(json.dumps(manifest))
        for name, collection in collections.items():
            store.delete_idx(collection, ids[name])
            assert not store.has_collection(collection)
        REDIS_CONN.REDIS.delete(queue)
        assert not REDIS_CONN.REDIS.exists(queue)
        for key in manifest["objects"]:
            client.remove_object(bucket, key)
        assert not list(client.list_objects(bucket, recursive=True))
        client.remove_bucket(bucket)
        assert not client.bucket_exists(bucket)
        with Session(bootstrapped_engine) as db:
            documents = list(db.scalars(sa.select(Document.id).where(Document.kb_id.in_([ids[k] for k in names]))))
            conditions = [
                (DocumentMetadata, DocumentMetadata.id.in_(documents)),
                (File2Document, File2Document.document_id.in_(documents)),
                (Task, Task.doc_id.in_(documents)),
                (Document, Document.id.in_(documents)),
                (File, File.id.in_(manifest.get("file_ids", []))),
                (Knowledgebase, Knowledgebase.id.in_([ids[k] for k in names])),
                (UserTenant, UserTenant.user_id.in_(list(ids.values()))),
                (Tenant, Tenant.id.in_([ids["owner"], ids["other"]])),
                (User, User.id.in_(list(ids.values()))),
            ]
            for model, condition in conditions:
                db.execute(sa.delete(model).where(condition))
            db.commit()
        with bootstrapped_engine.connect() as db:
            remaining = {model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model).where(condition)) for model, condition in conditions}
            assert not any(remaining.values()), remaining
        manifest.update(cleanup=True, remaining=remaining, bucket_absent=True, collections_absent=True, queue_absent=True, owned_document_ids=documents)
        manifest_path.write_text(json.dumps(manifest, indent=2))


def _snapshot(env: dict[str, Any]) -> dict[str, Any]:
    cfg = CONFIGS["milvus"]
    client = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
    try:
        indexes = {}
        for name, collection in env["collections"].items():
            if client.has_collection(collection):
                rows = client.query(collection, filter='pk != ""', output_fields=["*", "vector", "q_768_vec"], consistency_level="Strong")
                indexes[name] = {
                    "collection": client.describe_collection(collection),
                    "rows": sorted(rows, key=lambda row: row["pk"]),
                    "count": client.query(collection, filter='pk != ""', output_fields=["count(*)"], consistency_level="Strong"),
                }
            else:
                indexes[name] = None
    finally:
        client.close()
    with env["engine"].connect() as db:
        sql = {
            model.__tablename__: [dict(row) for row in db.execute(sa.select(model.__table__).order_by(model.id)).mappings()]
            for model in [User, Tenant, UserTenant, APIToken, Knowledgebase, Document, DocumentMetadata, File, File2Document, Task]
        }
    objects = {obj.object_name: _raw_object(env["client"], env["bucket"], obj.object_name) for obj in env["client"].list_objects(env["bucket"], recursive=True)}
    return {
        "sql": sql,
        "index": indexes,
        "objects": objects,
        "queue": {"entries": REDIS_CONN.REDIS.xrange(env["queue"]), "info": REDIS_CONN.REDIS.xinfo_stream(env["queue"]), "groups": REDIS_CONN.REDIS.xinfo_groups(env["queue"])}
        if REDIS_CONN.REDIS.exists(env["queue"])
        else None,
    }


async def test_image_read_real_storage_matrix_no_writes(image_resources: dict[str, Any], bootstrapped_async_engine: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    env, ids = image_resources, image_resources["ids"]
    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    documents: dict[str, str] = {}
    cases = {
        "png-wrong.jpg": (_image("PNG"), "image/png"),
        "jpeg": (_image("JPEG"), "image/jpeg"),
        "gif.png": (_image("GIF"), "image/gif"),
        "webp": (_image("WEBP"), "image/webp"),
        "bmp": (_image("BMP"), "image/bmp"),
        "empty": (b"", None),
        "html.png": (b"<html>image</html>", None),
        "svg.png": (b"<svg></svg>", None),
        "unknown.png": (b"unknown", None),
        "truncated.png": (b"\x89PNG\r\n\x1a\n", None),
        "truncated-real.jpg": (_image("JPEG")[:-20], None),
        "truncated-real.bmp": (_image("BMP")[:-8], None),
    }
    with Session(env["engine"]) as db:
        for key, (data, _) in cases.items():
            doc_id = documents[key] = uuid4().hex
            db.add(
                Document(
                    id=doc_id, kb_id=ids["kb"], created_by=ids["owner"], parser_id="naive", type="visual", name=key, thumbnail=key, status="0", chunk_num=2, token_num=11, meta_fields={"kept": True}
                )
            )
            env["storage"].put(ids["kb"], key, data)
        for key, thumbnail in [("inline", "data:image/png;base64,abc"), ("none", None), ("emptythumbnail", ""), ("missing-object", "missing-object")]:
            documents[key] = uuid4().hex
            db.add(Document(id=documents[key], kb_id=ids["kb"], created_by=ids["owner"], parser_id="naive", type="visual", thumbnail=thumbnail))
        for kb in ["sibling", "foreign", "inactivekb"]:
            doc_id = documents[kb] = uuid4().hex
            db.add(Document(id=doc_id, kb_id=ids[kb], created_by=ids["other"] if kb == "foreign" else ids["owner"], parser_id="naive", type="visual", thumbnail="other.png", status="0", chunk_num=1))
            env["storage"].put(ids[kb], "other.png", _image("PNG"))
        db.commit()
    env["storage"].put(ids["kb"], "unregistered.png", _image("PNG"))
    chunks: dict[str, list[dict[str, Any]]] = {}
    parent, child = uuid4().hex, uuid4().hex
    for kb in ["kb", "sibling", "foreign", "inactivekb"]:
        rows = []
        for chunk_id, available, mother, key, doc_id in (
            [
                (parent, 0, "", "chunk-image-multi-key.png", documents["png-wrong.jpg"]),
                (child, 0, parent, "child-image.png", documents["png-wrong.jpg"]),
                (uuid4().hex, 0, "", "foreign-doc.png", documents["foreign"]),
            ]
            if kb == "kb"
            else [(uuid4().hex, 0, "", "other.png", documents[kb])]
        ):
            rows.append(
                {
                    "id": chunk_id,
                    "pk": chunk_id,
                    "doc_id": doc_id,
                    "kb_id": ids[kb],
                    "img_id": f"{ids[kb]}-{key}",
                    "content_with_weight": "Read-only image registration",
                    "available_int": available,
                    "mom_id": mother,
                    "vector": [0.1] * 768,
                    "q_768_vec": [0.1] * 768,
                    "create_timestamp_flt": 1234.5,
                }
            )
            env["storage"].put(ids[kb], key, _image("PNG"))
        assert settings.docStoreConn.insert(rows, env["collections"][kb], ids[kb]) == []
        chunks[kb] = rows
    task_id, folder_id, file_id, relation_id = (uuid4().hex for _ in range(4))
    env["manifest"].update(file_ids=[folder_id, file_id], task_ids=[task_id], relation_ids=[relation_id])
    env["manifest_path"].write_text(json.dumps(env["manifest"], indent=2))
    with Session(env["engine"]) as db:
        db.add(File(id=folder_id, parent_id=folder_id, tenant_id=ids["owner"], created_by=ids["owner"], name="Images", type="folder"))
        db.add(File(id=file_id, parent_id=folder_id, tenant_id=ids["owner"], created_by=ids["owner"], name="Original image", type="visual", location="png-wrong.jpg", size=len(_image("PNG"))))
        db.add(File2Document(id=relation_id, file_id=file_id, document_id=documents["png-wrong.jpg"]))
        db.add(DocumentMetadata(id=documents["png-wrong.jpg"], tenant_id=ids["owner"], kb_id=ids["kb"], meta_fields={"preserved": "metadata"}))
        db.add(Task(id=task_id, doc_id=documents["png-wrong.jpg"], task_type="Parse", progress=0.25, digest="kept", chunk_ids=parent + " " + child))
        db.commit()
    assert REDIS_CONN.queue_product(env["queue"], {"doc_id": documents["png-wrong.jpg"], "id": task_id, "kept": True})
    async with sessions() as db:
        runtimes = {}
        for owner in ["owner", "other"]:
            upload = SimpleNamespace(filename="runtime.png", content_type="image/png", read=lambda: _image("PNG"))
            runtimes[owner] = await FileService.upload_info(db, ids[owner], upload)
        # Controlled server-side malformed descriptors/size; physical reads remain real.
        for kind in ["tampered", "size", "empty", "missing-sidecar"]:
            file_id = uuid4().hex
            raw = b"" if kind == "empty" else _image("PNG")
            descriptor = {**runtimes["owner"], "id": file_id, "created_by": ids["other"] if kind == "tampered" else ids["owner"], "size": len(raw) + (1 if kind == "size" else 0)}
            env["storage"].put(f"{ids['owner']}-downloads", file_id, raw)
            if kind != "missing-sidecar":
                env["storage"].put(f"{ids['owner']}-downloads", FileService._runtime_descriptor_key(file_id), json.dumps(descriptor).encode())
            runtimes[kind] = descriptor
    encrypted = EncryptedStorageWrapper(env["adapter"], key="scratch-image-key")
    encrypted.put(ids["kb"], "encrypted.png", _image("PNG"))
    env["adapter"].put(ids["kb"], "broken-encryption.png", b"RAGF" + b"0" * 19)
    with Session(env["engine"]) as db:
        for key in ["encrypted.png", "broken-encryption.png"]:
            documents[key] = uuid4().hex
            db.add(Document(id=documents[key], kb_id=ids["kb"], created_by=ids["owner"], parser_id="naive", type="visual", thumbnail=key))
        db.commit()
    env["manifest"].update(documents=documents, runtime_ids={kind: value["id"] for kind, value in runtimes.items()})
    env["manifest_path"].write_text(json.dumps(env["manifest"], indent=2))
    before = _snapshot(env)
    evidence = env["evidence"] / f"{ids['kb']}.readback.json"
    record: dict[str, Any] = {"before": before, "chunks_inserted": chunks, "runtimes": runtimes, "calls": []}
    evidence.write_text(json.dumps(record, default=_json, indent=2))

    def forbid(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("image read attempted a write")

    def sql_guard(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        assert statement.lstrip().split()[0].upper() not in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}

    async def dataset(
        db: AsyncSession, owner: str, key: str, expected: type[Exception] | None = None, kb: str = "kb", source: AuthenticationSource = AuthenticationSource.WEB_SESSION
    ) -> service.ImageBytes | None:
        image_id = f"{ids[kb]}-{key}"
        if expected:
            with pytest.raises(expected) as failure:
                await service.read_dataset_image(db, _principal(ids[owner], source), image_id)
            assert ids[kb] not in str(failure.value) and key not in str(failure.value)
            record["calls"].append({"owner": owner, "image_id": image_id, "error": type(failure.value).__name__, "message": str(failure.value)})
            return None
        result = await service.read_dataset_image(db, _principal(ids[owner], source), image_id)
        record["calls"].append({"owner": owner, "image_id": image_id, "media_type": result.media_type, "bytes": result.data})
        return result

    with monkeypatch.context() as guard:
        for target, attr in [
            (env["storage"], "put"),
            (env["storage"], "rm"),
            (settings.docStoreConn, "insert"),
            (settings.docStoreConn, "update"),
            (settings.docStoreConn, "delete"),
            (REDIS_CONN, "queue_product"),
        ]:
            guard.setattr(target, attr, forbid)
        sa.event.listen(bootstrapped_async_engine.sync_engine, "before_cursor_execute", sql_guard)
        try:
            async with sessions() as db:
                actor = _principal(ids["owner"])
                requested = [
                    documents["none"],
                    documents["png-wrong.jpg"],
                    documents["png-wrong.jpg"],
                    uuid4().hex,
                    documents["foreign"],
                    documents["inactivekb"],
                    documents["inline"],
                    documents["emptythumbnail"],
                ]
                mapping = await service.list_thumbnails(db, actor, requested)
                assert mapping == {
                    documents["none"]: None,
                    documents["png-wrong.jpg"]: f"/api/v1/documents/images/{ids['kb']}-png-wrong.jpg",
                    documents["inline"]: "data:image/png;base64,abc",
                    documents["emptythumbnail"]: "",
                }
                assert list(mapping) == [documents[key] for key in ["none", "png-wrong.jpg", "inline", "emptythumbnail"]]
                for role in ["owner", "normal", "admin"]:
                    for key, (data, mime) in cases.items():
                        result = await dataset(db, role, key, service.InvalidImageBytes if mime is None else None)
                        if result:
                            assert result.data == data and result.media_type == mime
                for role in ["invite", "inactive", "outsider", "other"]:
                    assert await service.list_thumbnails(db, _principal(ids[role]), [documents["png-wrong.jpg"]]) == {}
                    await dataset(db, role, "png-wrong.jpg", service.ImageUnavailable)
                await dataset(db, "owner", "png-wrong.jpg", source=AuthenticationSource.SDK_API_TOKEN)
                for key in ["chunk-image-multi-key.png", "child-image.png"]:
                    assert (await dataset(db, "owner", key)).data == _image("PNG")
                for key in ["foreign-doc.png", "unregistered.png", "missing-object", "unknown-key"]:
                    await dataset(db, "owner", key, service.ImageUnavailable)
                await dataset(db, "owner", "other.png", kb="sibling")
                await dataset(db, "owner", "other.png", service.ImageUnavailable, kb="foreign")
                await dataset(db, "owner", "other.png", service.ImageUnavailable, kb="inactivekb")
                await dataset(db, "other", "other.png", kb="foreign")
                assert (await service.read_dataset_image(db, actor, f"{ids['kb']}-png-wrong.jpg", ids["kb"])).media_type == "image/png"
                with pytest.raises(service.ImageUnavailable):
                    await service.read_dataset_image(db, actor, f"{ids['kb']}-png-wrong.jpg", ids["sibling"])
                for bad in ["", "-key", ids["kb"] + "-", "missinghyphen"]:
                    with pytest.raises(service.InvalidImageInput):
                        await service.read_dataset_image(db, actor, bad)
                for bad_ids in [[], [""], [documents["png-wrong.jpg"]] * 101]:
                    with pytest.raises(service.InvalidImageInput):
                        await service.list_thumbnails(db, actor, bad_ids)
                for owner in ["owner", "other"]:
                    result = await service.read_runtime_image(_principal(ids[owner]), runtimes[owner]["id"])
                    assert result.data == _image("PNG") and result.media_type == "image/png"
                    record["calls"].append({"runtime_owner": owner, "bytes": result.data, "media_type": result.media_type})
                with pytest.raises(service.ImageUnavailable):
                    await service.read_runtime_image(actor, runtimes["other"]["id"])
                for kind in ["tampered", "size", "empty", "missing-sidecar"]:
                    with pytest.raises(service.InvalidImageBytes if kind in {"size", "empty"} else service.ImageUnavailable):
                        await service.read_runtime_image(actor, runtimes[kind]["id"])
                # Controlled transport outage: real MinIO client against a closed endpoint.
                with monkeypatch.context() as fault:
                    fault.setattr(env["adapter"], "conn", Minio("127.0.0.1:1", access_key="scratch", secret_key="scratch-secret", secure=False))
                    await dataset(db, "owner", "png-wrong.jpg", service.ImageStorageFailure)
                # Real encrypted adapter and real invalid encrypted object (not a decrypt fake).
                with monkeypatch.context() as fault:
                    fault.setitem(resources._state, "storage", encrypted)
                    assert (await dataset(db, "owner", "png-wrong.jpg")).data == _image("PNG")
                    assert (await dataset(db, "owner", "encrypted.png")).data == _image("PNG")
                    await dataset(db, "owner", "broken-encryption.png", service.ImageStorageFailure)
                # Only-get legacy backends retain namespace/decryption compatibility.
                legacy_adapter = SimpleNamespace(**{name: getattr(env["adapter"], name) for name in ["put", "get", "rm", "obj_exist", "health"]})
                legacy_encrypted = EncryptedStorageWrapper(legacy_adapter, key="scratch-image-key")
                with monkeypatch.context() as fault:
                    fault.setitem(resources._state, "storage", legacy_encrypted)
                    assert (await dataset(db, "owner", "encrypted.png")).data == _image("PNG")
        finally:
            sa.event.remove(bootstrapped_async_engine.sync_engine, "before_cursor_execute", sql_guard)
    after = _snapshot(env)
    record["after"] = after
    record["unchanged"] = before == after
    evidence.write_text(json.dumps(record, default=_json, indent=2))
    assert before == after
