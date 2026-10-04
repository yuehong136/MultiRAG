"""Real service-entry reads: isolated SQL, MinIO, Milvus and Redis, no HTTP.

Snapshot every relevant SQL column, full index payload/vectors/schema/count,
physical object bytes and owned Redis stream before/after the read matrix.
"""

import base64
import copy
import json
import os
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import sqlalchemy as sa
from minio import Minio
from PIL import Image
from pymilvus import MilvusClient
from sqlalchemy.orm import Session

from api.db.db_models import APIToken, Document, DocumentMetadata, File, File2Document, Knowledgebase, Task, Tenant, User, UserTenant
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
    with ExitStack() as cleanup:
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

        def remove_bucket() -> None:
            if client.bucket_exists(bucket):
                for item in client.list_objects(bucket, recursive=True):
                    client.remove_object(bucket, item.object_name)
                assert not list(client.list_objects(bucket, recursive=True))
                client.remove_bucket(bucket)
            assert not client.bucket_exists(bucket)
            manifest["bucket_absent"] = True

        def remove_rows() -> None:
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
            manifest.update(remaining=remaining, owned_document_ids=documents)

        def remove_collections() -> None:
            for name, collection in collections.items():
                store.delete_idx(collection, ids[name])
                assert not store.has_collection(collection)
            manifest["collections_absent"] = True

        def remove_queue() -> None:
            REDIS_CONN.REDIS.delete(queue)
            assert not REDIS_CONN.REDIS.exists(queue)
            manifest["queue_absent"] = True

        def record_cleanup() -> None:
            manifest["cleanup"] = all(manifest.get(key) for key in ["bucket_absent", "collections_absent", "queue_absent"]) and not any(manifest.get("remaining", {"unknown": 1}).values())
            manifest_path.write_text(json.dumps(manifest, indent=2))

        cleanup.callback(record_cleanup)
        cleanup.callback(remove_rows)
        cleanup.callback(remove_bucket)
        cleanup.callback(remove_queue)
        cleanup.callback(remove_collections)
        client.make_bucket(bucket)
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

        def record_snapshot() -> None:
            (evidence / f"{ids['kb']}.before-cleanup.json").write_text(json.dumps(_snapshot(env), default=_json, indent=2))
            manifest["objects"] = [obj.object_name for obj in client.list_objects(bucket, recursive=True)]
            manifest_path.write_text(json.dumps(manifest))

        cleanup.callback(record_snapshot)
        yield env


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
