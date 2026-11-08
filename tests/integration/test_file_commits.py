"""File snapshot acceptance against scratch PostgreSQL and owned MinIO objects."""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI
from minio import Minio
from minio.error import S3Error
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.apps.restful_apis.file_commit_api import router, service
from api.db.db_models import Conversation, Dialog, Document, File, File2Document, FileCommit, Knowledgebase, Tenant, User, UserTenant, get_async_db
from api.db.services.file_commit_service import CommitError, CreateCommit, FileCommitService, build_tree, diff_trees
from common.config_utils import CONFIGS
from tests.support.resource_journal import ResourceJournal


class Objects:
    """Real MinIO adapter with reserve-before-put and strict independent readback."""

    def __init__(self, client: Minio, reader: Minio, bucket: str, journal: ResourceJournal) -> None:
        self.client, self.reader, self.bucket, self.journal = client, reader, bucket, journal
        self.fail_put = False
        self.keys: set[str] = set()

    def put(self, bucket: str, key: str, data: bytes) -> None:
        from io import BytesIO

        path = bucket + "/" + key
        self.journal.event("reserve-object", bucket=self.bucket, key=path, sha256=hashlib.sha256(data).hexdigest())
        self.keys.add(path)
        self.client.put_object(self.bucket, path, BytesIO(data), len(data))
        if self.fail_put:
            raise OSError("injected failure after actual object write")

    def get_bytes(self, bucket: str, key: str) -> bytes | None:
        try:
            response = self.reader.get_object(self.bucket, bucket + "/" + key)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                return None
            raise
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def obj_exist(self, bucket: str, key: str) -> bool:
        return self.get_bytes(bucket, key) is not None

    def move(self, source: str, key: str, destination: str, new_key: str) -> None:
        data = self.get_bytes(source, key)
        assert isinstance(data, bytes)
        self.put(destination, new_key, data)
        self.rm(source, key)

    def rm(self, bucket: str, key: str) -> None:
        path = bucket + "/" + key
        self.client.remove_object(self.bucket, path)
        absent = self.get_bytes(bucket, key) is None
        self.journal.event("object-cleanup-readback", bucket=self.bucket, key=path, absent=absent)
        assert absent


@pytest.fixture
def objects() -> Iterator[Objects]:
    config = CONFIGS["minio"]
    kwargs = {"access_key": config["user"], "secret_key": str(config["password"]), "secure": config.get("secure", False)}
    client, reader = Minio(config["host"], **kwargs), Minio(config["host"], **kwargs)
    journal = ResourceJournal("file-commit-minio")
    bucket = "f-commits-" + uuid4().hex
    journal.event("reserve-bucket", bucket=bucket, endpoint=config["host"])
    store = Objects(client, reader, bucket, journal)
    try:
        client.make_bucket(bucket)
        yield store
    finally:
        # All keys are under our unique bucket; reader is a separate SDK client.
        if client.bucket_exists(bucket):
            for key in sorted(store.keys):
                client.remove_object(bucket, key)
            remaining = list(reader.list_objects(bucket, recursive=True))
            assert not remaining
            client.remove_bucket(bucket)
        absent = not reader.bucket_exists(bucket)
        journal.event("bucket-cleanup-readback", bucket=bucket, absent=absent)
        assert absent


@pytest.fixture
async def state(bootstrapped_async_engine: Any, objects: Objects) -> AsyncIterator[dict[str, Any]]:
    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    ids = {key: uuid4().hex for key in ("tenant", "user", "other", "foreign", "root", "folder", "nested", "file", "second")}
    journal = ResourceJournal("file-commit-sql")
    journal.event("reserve-sql-fixture", ids=ids, database=bootstrapped_async_engine.url.database)
    async with sessions() as db:
        db.add(UserTenant(id=uuid4().hex, tenant_id=ids["tenant"], user_id=ids["user"], role="owner", status="1", invited_by=ids["user"]))
        db.add(UserTenant(id=uuid4().hex, tenant_id=ids["tenant"], user_id=ids["other"], role="normal", status="1", invited_by=ids["user"]))
        for key, parent, name in (("root", "root", "/"), ("folder", "root", "work"), ("nested", "folder", "nested")):
            db.add(File(id=ids[key], tenant_id=ids["tenant"], parent_id=ids[parent], created_by=ids["user"], name=name, type="folder", size=0, source_type=""))
        db.add(File(id=ids["file"], tenant_id=ids["tenant"], parent_id=ids["nested"], created_by=ids["user"], name="a.txt", type="other", size=3, location="a.txt", source_type=""))
        await db.commit()
    objects.put(ids["nested"], "a.txt", b"old")
    yield {**ids, "sessions": sessions, "objects": objects, "journal": journal}
    # Scratch DB is disposed/dropped by the shared fixture, independently read back.


async def create(env: dict[str, Any], files: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    async with env["sessions"]() as db:
        return await FileCommitService(db, env["objects"]).create("folders", env["folder"], env["tenant"], env["user"], CreateCommit(message="snapshot", files=files, **kwargs))


async def test_full_tree_inheritance_diff_delete_and_content(state: dict[str, Any]) -> None:
    env = state
    first = await create(env, [])
    second = await create(env, [{"file_id": env["second"], "operation": "add", "file_name": "empty.txt", "content": ""}], parent_id=first["id"])
    third = await create(env, [{"file_id": env["file"], "operation": "rename", "new_name": "renamed.txt"}])
    async with env["sessions"]() as db:
        svc = FileCommitService(db, env["objects"])
        folder = await svc.resolve("folders", env["folder"], env["tenant"], env["user"])
        before, after = await svc.commit(folder, first["id"]), await svc.commit(folder, second["id"])
        assert [(r["file_id"], r["operation"]) for r in diff_trees(before.tree_state, after.tree_state)] == [(env["second"], "add")]
        assert await svc.content(after, env["file"]) == b"old"  # inherited, no delta item
        assert await svc.content(after, env["second"]) == b""
        assert after.tree_state[env["file"]]["location"] == before.tree_state[env["file"]]["location"]
        original_tree = build_tree(before.tree_state, folder.id)
        nested = await db.get(File, env["nested"])
        nested.name = "today"
        await db.commit()
        assert build_tree(before.tree_state, folder.id) == original_tree
    deleted = await create(env, [{"file_id": env["file"], "operation": "delete"}])
    async with env["sessions"]() as reader:
        assert await reader.get(File, env["file"]) is None
        svc = FileCommitService(reader, env["objects"])
        folder = await svc.resolve("folders", env["folder"], env["tenant"], env["user"])
        old = await svc.commit(folder, third["id"])
        assert await svc.content(old, env["file"]) == b"old"
        assert env["file"] not in (await svc.commit(folder, deleted["id"])).tree_state
        versions = await svc.versions(env["tenant"], env["user"], env["file"])
        assert versions[0]["operation"] == "delete"
        assert {row["operation"] for row in versions} == {"add", "rename", "delete"}


async def test_partial_object_failure_sql_failure_and_stale_head(state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = state
    first = await create(env, [])
    baseline_keys = {entry["location"] for entry in first["tree_state"].values() if entry["type"] == "file"}
    env["objects"].fail_put = True
    with pytest.raises(OSError):
        await create(env, [{"file_id": env["file"], "operation": "modify", "content": "bad"}])
    env["objects"].fail_put = False
    async with env["sessions"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(FileCommit).where(FileCommit.folder_id == env["folder"])) == 1
        live = await reader.get(File, env["file"])
        assert live.location == "a.txt" and live.size == 3
        assert await FileCommitService(reader, env["objects"]).content(await reader.get(FileCommit, first["id"]), env["file"]) == b"old"
    # Inject a genuine SQL constraint failure after external writes.
    original = AsyncSession.flush

    async def fail_flush(self: AsyncSession, *args: Any, **kwargs: Any) -> None:
        if any(isinstance(row, FileCommit) for row in self.new):
            await self.execute(sa.text("SELECT 1/0"))
        await original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "flush", fail_flush)
        with pytest.raises(sa.exc.DBAPIError):
            await create(env, [{"file_id": env["file"], "operation": "modify", "content": "sqlfail"}])
    for key in env["objects"].keys:
        if any(namespace in key for namespace in (".file-commits/", ".file-working/")) and not any(key.endswith(location) for location in baseline_keys):
            bucket, path = key.split("/", 1)
            assert env["objects"].get_bytes(bucket, path) is None
    with pytest.raises(CommitError, match="head changed"):
        await create(env, [], parent_id="stale")
    async with env["sessions"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(FileCommit).where(FileCommit.folder_id == env["folder"])) == 1
        assert (await reader.get(File, env["file"])).location == "a.txt"


async def test_concurrent_parent_compare_and_serial_chain(state: dict[str, Any]) -> None:
    env = state
    first = await create(env, [])

    async def contender(content: str) -> Any:
        try:
            return await create(env, [{"file_id": env["file"], "operation": "modify", "content": content}], parent_id=first["id"])
        except CommitError as exc:
            return exc

    result = await asyncio.gather(contender("winner a"), contender("winner b"))
    assert sum(isinstance(row, dict) for row in result) == 1
    assert sum(isinstance(row, CommitError) and row.status == 409 for row in result) == 1
    await asyncio.gather(create(env, []), create(env, []))
    async with env["sessions"]() as reader:
        commits = list(await reader.scalars(sa.select(FileCommit).where(FileCommit.folder_id == env["folder"]).order_by(FileCommit.sequence)))
        assert [row.sequence for row in commits] == [1, 2, 3, 4]
        assert [row.parent_id for row in commits] == [None, *[row.id for row in commits[:-1]]]
        for row in commits:
            assert await FileCommitService(reader, env["objects"]).content(row, env["file"])


async def test_api_scope_http_sql_object_roundtrip(state: dict[str, Any]) -> None:
    env = state
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    from api.apps.restful_apis.chat_api import router as chat_router

    app.include_router(chat_router, prefix="/api/v1")

    async def db_dependency() -> AsyncIterator[AsyncSession]:
        async with env["sessions"]() as db:
            yield db

    from api.apps import manager

    async with env["sessions"]() as db:
        for identity in (env["user"], env["other"], env["foreign"]):
            db.add(Tenant(id=identity, name="File HTTP", llm_id="test", embd_id="test", asr_id="test", img2txt_id="test", parser_ids="naive", status="1"))
            db.add(User(id=identity, nickname="File HTTP", email=identity + "@file-commit.test", status="1"))
            db.add(UserTenant(id=uuid4().hex, user_id=identity, tenant_id=identity, role="owner", status="1", invited_by=identity))
        await db.commit()
    tokens = {identity: manager.create_access_token(data={"sub": identity + "@file-commit.test"}) for identity in (env["user"], env["other"], env["foreign"])}

    def svc(db: AsyncSession = Depends(get_async_db)) -> FileCommitService:
        return FileCommitService(db, env["objects"])

    app.dependency_overrides[get_async_db] = db_dependency
    app.dependency_overrides[service] = svc
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = "/api/v1/workspaces/" + env["folder"]
        assert (await client.get(path + "/commits")).status_code == 401
        client.headers["Authorization"] = "Bearer " + tokens[env["user"]]
        response = await client.post(path + "/commits", json={"message": "http", "files": []})
        assert response.status_code == 200, response.text
        commit = response.json()["data"]["id"]
        for suffix in ("/commits", f"/commits/{commit}", f"/commits/{commit}/files", f"/commits/{commit}/tree", f"/commits/diff?from={commit}&to={commit}", "/changes"):
            assert (await client.get(path + suffix)).json()["code"] == 0
        assert (await client.get(path + f"/commits/{commit}/files/{env['file']}/content")).json()["data"]["content"] == "old"
        assert (await client.get(f"/api/v1/files/{env['file']}/versions")).json()["code"] == 0
        assert (await client.post(path + "/commits", json={"message": "bad", "files": [{"file_id": env["file"], "operation": "delete", "unknown": 1}]})).status_code == 422
        chat_id, session_id = uuid4().hex, uuid4().hex
        env["journal"].event("reserve-session-unknown-field-regression", chat_id=chat_id, session_id=session_id)
        async with env["sessions"]() as db:
            db.add(Dialog(id=chat_id, tenant_id=env["user"], name="Session strictness", llm_id="unused"))
            db.add(Conversation(id=session_id, dialog_id=chat_id, user_id=env["user"], name="unchanged", message=[], reference=[]))
            await db.commit()
        bad_session = await client.patch(f"/api/v1/chats/{chat_id}/sessions/{session_id}", json={"unknown_key": "unknown_value"})
        assert bad_session.status_code == 200
        assert bad_session.json() == {"code": 100, "message": 'Unrecognized field name: "unknown_key"'}
        async with env["sessions"]() as reader:
            session = await reader.get(Conversation, session_id)
            assert session.name == "unchanged" and session.message == [] and session.reference == []
        # A real JWT has personal tenant U; the target folder belongs to T != U.
        client.headers["Authorization"] = "Bearer " + tokens[env["other"]]
        assert (await client.get(path + "/commits")).status_code == 200
        # Private dataset metadata, artifacts and all aliases share one boundary.
        kb_id, kb_folder, doc_id, artifact = (uuid4().hex for _ in range(4))
        env["journal"].event("reserve-dataset-fixture", ids=[kb_id, kb_folder, doc_id, artifact])
        async with env["sessions"]() as db:
            db.add(Knowledgebase(id=kb_id, tenant_id=env["tenant"], created_by=env["user"], name=kb_id, permission="me", embd_id="test", status="1"))
            db.add(File(id=kb_folder, tenant_id=env["tenant"], parent_id=env["root"], created_by=env["user"], name=kb_id, type="folder", source_type="knowledgebase", size=0))
            db.add(Document(id=doc_id, kb_id=kb_id, created_by=env["user"], name="artifact.txt", type="other", parser_id="naive", location="artifact.txt", size=7))
            db.add(
                File(id=artifact, tenant_id=env["tenant"], parent_id=kb_folder, created_by=env["user"], name="artifact.txt", type="other", source_type="knowledgebase", location="artifact.txt", size=7)
            )
            db.add(File2Document(id=uuid4().hex, file_id=artifact, document_id=doc_id))
            await db.commit()
        env["objects"].put(kb_id, "artifact.txt", b"private")
        dataset_path = "/api/v1/datasets/" + kb_id
        assert (await client.get(dataset_path + "/commits")).status_code == 404
        assert (await client.get("/api/v1/folders/" + kb_folder + "/commits")).status_code == 404
        client.headers["Authorization"] = "Bearer " + tokens[env["user"]]
        saved = await client.post(dataset_path + "/commits", json={"message": "dataset", "files": []})
        assert saved.status_code == 200, saved.text
        dataset_commit = saved.json()["data"]["id"]
        assert (await client.get(dataset_path + f"/commits/{dataset_commit}/files/{artifact}/content")).json()["data"]["content"] == "private"
        client.headers["Authorization"] = "Bearer " + tokens[env["other"]]
        assert (await client.get(f"/api/v1/files/{artifact}/versions")).status_code == 404
        async with env["sessions"]() as db:
            kb = await db.get(Knowledgebase, kb_id)
            kb.permission = "team"
            await db.commit()
        assert (await client.get(dataset_path + "/commits")).status_code == 200
        assert (await client.get(dataset_path + f"/commits/{dataset_commit}/files/{artifact}/content")).json()["data"]["content"] == "private"
        assert (await client.post(dataset_path + "/commits", json={"message": "no-write", "files": []})).status_code == 403
        async with env["sessions"]() as db:
            membership = await db.scalar(sa.select(UserTenant).where(UserTenant.tenant_id == env["tenant"], UserTenant.user_id == env["other"]))
            membership.role = "admin"
            await db.commit()
        assert (await client.post(dataset_path + "/commits", json={"message": "admin-snapshot", "files": []})).status_code == 200
        assert (await client.post(dataset_path + "/commits", json={"message": "managed-delete", "files": [{"file_id": artifact, "operation": "delete"}]})).status_code == 409
        async with env["sessions"]() as db:
            membership = await db.scalar(sa.select(UserTenant).where(UserTenant.tenant_id == env["tenant"], UserTenant.user_id == env["other"]))
            membership.status = "0"
            await db.commit()
        assert (await client.get(dataset_path + "/commits")).status_code == 404
        client.headers["Authorization"] = "Bearer " + tokens[env["foreign"]]
        for suffix in ("/commits", f"/commits/{commit}", f"/commits/{commit}/tree", f"/commits/{commit}/files/{env['file']}/content"):
            assert (await client.get(path + suffix)).status_code == 404
        assert (await client.post(path + "/commits", json={"message": "denied", "files": []})).status_code == 404
        assert (await client.get(f"/api/v1/files/{env['file']}/versions")).status_code == 404


async def test_ordinary_files_move_delete_preserves_history(state: dict[str, Any], bootstrapped_engine: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.orm import Session

    from api.apps.services import file_api_service
    from common import resources

    env = state
    committed = await create(env, [{"file_id": env["second"], "operation": "add", "file_name": "working.txt", "content": "historical"}])
    snapshot = committed["tree_state"][env["second"]]
    async with env["sessions"]() as reader:
        live = await reader.get(File, env["second"])
        source, working = live.parent_id, live.location
        assert working != snapshot["location"] and working.startswith(".file-working/")
    monkeypatch.setitem(resources._state, "storage", env["objects"])

    def ordinary_lifecycle() -> None:
        with Session(bootstrapped_engine) as db:
            ok, result = file_api_service.move_files(db, env["tenant"], [env["second"]], env["nested"])
            assert ok, result
            moved = db.get(File, env["second"])
            assert moved.parent_id == env["nested"]
            ok, result = file_api_service.delete_files(db, env["tenant"], [env["second"]])
            assert ok, result

    await asyncio.to_thread(ordinary_lifecycle)
    assert env["objects"].get_bytes(source, working) is None
    assert env["objects"].get_bytes(snapshot["bucket"], snapshot["location"]) == b"historical"
    async with env["sessions"]() as reader:
        assert await reader.get(File, env["second"]) is None
        saved = await reader.get(FileCommit, committed["id"])
        assert await FileCommitService(reader, env["objects"]).content(saved, env["second"]) == b"historical"


async def test_cross_tenant_ancestors_scope_and_failed_delete(state: dict[str, Any]) -> None:
    env = state
    first = await create(env, [])
    bad, foreign_root, foreign_child = (uuid4().hex for _ in range(3))
    env["journal"].event("reserve-invalid-ancestry", ids=[bad, foreign_root, foreign_child])
    async with env["sessions"]() as db:
        db.add(File(id=foreign_root, tenant_id=env["foreign"], parent_id=foreign_root, created_by=env["foreign"], name="foreign", type="folder", source_type="", size=0))
        db.add(File(id=bad, tenant_id=env["tenant"], parent_id=foreign_root, created_by=env["user"], name="bad", type="folder", source_type="", size=0))
        db.add(File(id=foreign_child, tenant_id=env["foreign"], parent_id=env["folder"], created_by=env["foreign"], name="foreign child", type="other", location="missing", source_type="", size=1))
        await db.commit()
    async with env["sessions"]() as db:
        svc = FileCommitService(db, env["objects"])
        with pytest.raises(CommitError):
            await svc.resolve("folders", bad, env["tenant"], env["user"])
        folder = await svc.resolve("folders", env["folder"], env["tenant"], env["user"])
        with pytest.raises(CommitError, match="ancestry"):
            await svc.rows(folder)
        nested = await svc.resolve("folders", env["nested"], env["tenant"], env["user"])
        with pytest.raises(CommitError, match="Commit not found"):
            await svc.commit(nested, first["id"])
    # A later invalid item cannot leave an earlier delete committed.
    with pytest.raises(CommitError):
        await create(env, [{"file_id": env["file"], "operation": "delete"}, {"file_id": foreign_child, "operation": "modify", "content": "forbidden"}])
    async with env["sessions"]() as reader:
        assert await reader.get(File, env["file"]) is not None
        assert await reader.scalar(sa.select(sa.func.count()).select_from(FileCommit).where(FileCommit.folder_id == env["folder"])) == 1
