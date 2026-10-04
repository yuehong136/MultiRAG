"""Scratch PostgreSQL acceptance for assets, deferred bindings and leased effects."""

import io
import uuid
import zipfile
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.db.db_models import Skill, SkillIndexGeneration, SkillOperation, SkillSpace, SkillVersion
from api.skills.package import validate_package
from api.skills.schemas import ActivateVersion, CreateSpace, SkillError, UploadManifest
from api.skills.service import SkillService
from api.skills.storage import SkillStorage
from api.skills.worker import SkillWorker


@pytest.fixture(autouse=True)
async def isolate_worker_queue(bootstrapped_async_engine: Any) -> AsyncIterator[None]:
    """Discard this test's durable jobs only after assertions, within scratch PG.

    Several fault tests intentionally leave pending jobs. A later worker must not
    claim them using a different test's object-store fake.
    """
    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    async with sessions() as db:
        previous = list(await db.scalars(sa.select(SkillOperation.id)))
    yield
    async with sessions() as db:
        await db.execute(sa.delete(SkillOperation).where(SkillOperation.id.not_in(previous)))
        await db.commit()


class MemoryObjects:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put(self, bucket: str, key: str, data: bytes) -> None:
        self.objects[bucket, key] = data

    def get(self, bucket: str, key: str) -> bytes:
        return self.objects[bucket, key]

    def get_bytes(self, bucket: str, key: str) -> bytes | None:
        return self.objects.get((bucket, key))

    def rm(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)

    def obj_exist(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.objects


class NoModels:
    async def delete(self, name: str, version_ids: list[str] | None = None) -> None:
        raise AssertionError("Unindexed assets must not invent an index")


@pytest.mark.asyncio
async def test_install_download_activate_lease_and_isolation(bootstrapped_async_engine: Any) -> None:
    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant = uuid.uuid4().hex
    objects = MemoryObjects()
    storage = SkillStorage(objects)
    search = NoModels()
    async with sessions() as db:
        service = SkillService(db, storage, search)
        space = await service.create_space(tenant, CreateSpace(name="Demo " + tenant))
        inputs = [("SKILL.md", b"---\nname: demo\ndescription: Useful demo\ntags: [one]\n---\nText"), ("scripts/nested/file.txt", b"nested byte value")]
        package = validate_package(UploadManifest(name="demo", version="1.0.0"), inputs)
        result = await service.install(tenant, space["id"], package, True, "install-demo")
        operation_id = result["operation_id"]
        assert result["state"] == "pending"
        operation = await service.operation(tenant, operation_id)
        version_id, skill_id = operation.resource_id, operation.payload["skill_id"]
        assert operation.phase == "sealed"
        assert (await service.install(tenant, space["id"], package, True, "install-demo"))["operation_id"] == operation_id
        assert (await service.install(tenant, space["id"], package, True, "install-demo-new-key"))["operation_id"] == operation_id
        with pytest.raises(SkillError) as conflict:
            await service.install(tenant, space["id"], package, False, "different-activation")
        assert conflict.value.status == 409 and conflict.value.code == "VERSION_ALREADY_INSTALLED"
        with pytest.raises(SkillError) as same_key:
            await service.install(tenant, space["id"], package, False, "install-demo")
        assert same_key.value.code == "IDEMPOTENCY_CONFLICT"

        with pytest.raises(SkillError) as hidden:
            await service.space(uuid.uuid4().hex, space["id"])
        assert hidden.value.status == 404
        await db.rollback()
    worker = SkillWorker(sessions, storage, search, None)
    assert await worker.run_once()
    async with sessions() as db:
        service = SkillService(db, storage, search)
        operation = await service.operation(tenant, operation_id)
        assert operation.state == "succeeded", operation.error
        assert operation.result["index_state"] == "unindexed"
        detail = await service.get_skill(tenant, space["id"], skill_id)
        assert detail["skill"]["active_version_id"] == version_id
        content, media = await service.content(tenant, space["id"], version_id)
        assert media == "application/zip"
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            assert archive.read("scripts/nested/file.txt") == inputs[1][1]
        with pytest.raises(SkillError, match="Deactivate"):
            await service.register_action(tenant, space["id"], "delete_version", "delete-active", version_id)
        await db.rollback()
        detail = await service.get_skill(tenant, space["id"], skill_id)
        action = await service.register_action(tenant, space["id"], "activate", "deactivate", skill_id, ActivateVersion(version_id=None, revision=detail["skill"]["revision"]))
    # Two workers must never acquire the same lease. Expiry permits fenced recovery.
    lease = await worker.claim()
    assert lease and lease.id == action["operation_id"]
    other = SkillWorker(sessions, storage, search, None)
    assert await other.claim() is None
    async with sessions() as db:
        await db.execute(sa.update(SkillOperation).where(SkillOperation.id == lease.id).values(lease_expires_at=sa.func.now() - sa.text("interval '1 second'")))
        await db.commit()
    new_lease = await other.claim()
    assert new_lease and new_lease.revision > lease.revision
    async with sessions() as db:
        with pytest.raises(SkillError, match="lease"):
            await worker.locked(db, lease)
    await other.process(new_lease)


@pytest.mark.asyncio
async def test_deferred_foreign_keys_reject_cross_resource_bindings(bootstrapped_async_engine: Any) -> None:
    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant = uuid.uuid4().hex
    async with sessions() as db:
        service = SkillService(db, SkillStorage(MemoryObjects()), NoModels())
        first = await service.create_space(tenant, CreateSpace(name="First " + tenant))
        second = await service.create_space(tenant, CreateSpace(name="Second " + tenant))
        generation = SkillIndexGeneration(
            id=uuid.uuid4().hex, tenant_id=tenant, space_id=first["id"], config_revision=1, source_revision=1, config={}, dimension=0, index_name="skill_" + uuid.uuid4().hex, state="building"
        )
        db.add(generation)
        await db.commit()
        generation_id = generation.id
        second_row = await db.get(SkillSpace, second["id"])
        second_row.active_generation_id = generation_id
        with pytest.raises(sa.exc.IntegrityError):
            await db.commit()
        await db.rollback()
        # Direct SQL bypasses HTTP validation; tenant binding still rejects it.
        db.add(Skill(id=uuid.uuid4().hex, tenant_id=uuid.uuid4().hex, space_id=first["id"], folder_id=uuid.uuid4().hex, name="bad", description="", tags=[], state="active", revision=1))
        with pytest.raises(sa.exc.IntegrityError):
            await db.commit()
        await db.rollback()
        skills = []
        for name in ("one", "two"):
            skill = Skill(id=uuid.uuid4().hex, tenant_id=tenant, space_id=first["id"], folder_id=uuid.uuid4().hex, name=name, description="", tags=[], state="active", revision=1)
            db.add(skill)
            skills.append(skill)
        await db.flush()
        version = SkillVersion(
            id=uuid.uuid4().hex,
            tenant_id=tenant,
            skill_id=skills[0].id,
            folder_id=uuid.uuid4().hex,
            version="1.0.0",
            content_digest="a" * 64,
            manifest={},
            source_kind="local",
            state="installed",
            index_state="unindexed",
            file_count=0,
            total_size=0,
        )
        db.add(version)
        await db.commit()
        skills[1].active_version_id = version.id
        with pytest.raises(sa.exc.IntegrityError):
            await db.commit()
        await db.rollback()
    async with bootstrapped_async_engine.connect() as connection:
        rows = (await connection.execute(sa.text("SELECT conname, condeferrable, condeferred FROM pg_constraint WHERE conname IN ('fk_skill_space_generation','fk_skill_active_version')"))).all()
        assert len(rows) == 2
        assert all(row.condeferrable and row.condeferred for row in rows)


@pytest.mark.asyncio
async def test_cleanup_keeps_persisted_addresses_across_file_sql_loss(bootstrapped_async_engine: Any) -> None:
    """A legacy cleanup can commit SQL before the process or object deletion fails."""
    from api.db.db_models import File, SkillVersionFile

    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant = uuid.uuid4().hex
    objects = MemoryObjects()
    storage = SkillStorage(objects)
    search = NoModels()
    async with sessions() as db:
        service = SkillService(db, storage, search)
        space = await service.create_space(tenant, CreateSpace(name="Cleanup " + tenant))
        package = validate_package(UploadManifest(name="cleanup", version="1.0.0"), [("SKILL.md", b"---\nname: cleanup\ndescription: cleanup\n---\nTest"), ("nested/deep/asset.bin", b"\xff")])
        installed = await service.install(tenant, space["id"], package, False, "cleanup-install")
        operation = await service.operation(tenant, installed["operation_id"])
        version_id = operation.resource_id
    worker = SkillWorker(sessions, storage, search, None)
    assert await worker.run_once()
    async with sessions() as db:
        service = SkillService(db, storage, search)
        removed = await service.register_action(tenant, space["id"], "delete_version", "cleanup-delete", version_id)

    async def delete_sql_only(uid: str, ids: list[str]) -> None:
        async with sessions() as db:
            tree = sa.select(File.id).where(File.id.in_(ids), File.tenant_id == uid).cte("delete_tree", recursive=True)
            tree = tree.union(sa.select(File.id).join(tree, File.parent_id == tree.c.id).where(File.tenant_id == uid))
            all_ids = list(await db.scalars(sa.select(tree.c.id)))
            await db.execute(sa.delete(File).where(File.id.in_(all_ids)))
            await db.commit()

    worker.cleanup = delete_sql_only
    original_rm = objects.rm

    def failure(bucket: str, key: str) -> None:
        raise OSError("provider unavailable")

    objects.rm = failure
    assert await worker.run_once()
    async with sessions() as db:
        service = SkillService(db, storage, search)
        operation = await service.operation(tenant, removed["operation_id"])
        assert operation.state == "failed"
        assert operation.payload["cleanup_records"][version_id]["objects"]
        assert await db.scalar(sa.select(sa.func.count()).select_from(SkillVersionFile).where(SkillVersionFile.version_id == version_id)) == 2
        await service.retry(tenant, operation.id)
    objects.rm = original_rm
    assert await worker.run_once()
    assert objects.objects == {}
    async with sessions() as db:
        operation = await db.get(SkillOperation, removed["operation_id"])
        version = await db.get(SkillVersion, version_id)
        assert operation.state == "succeeded", operation.result
        assert version.state == "deleted"
        assert await db.scalar(sa.select(sa.func.count()).select_from(SkillVersionFile).where(SkillVersionFile.version_id == version_id)) == 0


@pytest.mark.asyncio
async def test_migration_and_single_table_bootstrap_add_deferred_constraints(pg_scratch_engine: Any) -> None:
    """Mirror both existing DB migration and production's single-table bootstrap."""
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from api.db.db_models import SkillSearchConfig, SkillVersionFile, ensure_skill_deferred_constraints

    spec = importlib.util.spec_from_file_location("skill_assets_migration", Path(__file__).parents[2] / "configs/alembic/versions/c5e7f9a1b3d5_add_skill_assets.py")
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    models = (SkillSpace, Skill, SkillVersion, SkillVersionFile, SkillSearchConfig, SkillIndexGeneration, SkillOperation)
    with pg_scratch_engine.begin() as connection:
        for model in reversed(models):
            connection.execute(sa.text(f'DROP TABLE IF EXISTS usr_ai."{model.__tablename__}" CASCADE'))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()
        assert {fk["name"] for fk in sa.inspect(connection).get_foreign_keys("t_ai_skill_spaces", schema="usr_ai")} >= {"fk_skill_space_generation"}
        assert {fk["name"] for fk in sa.inspect(connection).get_foreign_keys("t_ai_skills", schema="usr_ai")} >= {"fk_skill_active_version"}
        for model in reversed(models):
            connection.execute(sa.text(f'DROP TABLE usr_ai."{model.__tablename__}" CASCADE'))
        for model in models:
            model.__table__.create(connection)
        ensure_skill_deferred_constraints(connection)
        ensure_skill_deferred_constraints(connection)
        rows = connection.execute(sa.text("SELECT conname, condeferrable, condeferred FROM pg_constraint WHERE conname IN ('fk_skill_space_generation','fk_skill_active_version')")).all()
        assert len(rows) == 2 and all(row.condeferrable and row.condeferred for row in rows)
        with Operations.context(context):
            # Reject missing and same-name changed constraints, not merely bad columns.
            for mutation in (
                "ALTER TABLE usr_ai.t_ai_skills DROP CONSTRAINT fk_skill_space",
                "ALTER TABLE usr_ai.t_ai_skill_versions DROP CONSTRAINT uq_skill_version_name",
                "DROP INDEX usr_ai.uq_skill_live_name",
                "ALTER TABLE usr_ai.t_ai_skills DROP CONSTRAINT ck_skill_revision",
                "ALTER TABLE usr_ai.t_ai_skills DROP CONSTRAINT ck_skill_revision; ALTER TABLE usr_ai.t_ai_skills ADD CONSTRAINT ck_skill_revision CHECK (revision >= 0)",
            ):
                savepoint = connection.begin_nested()
                for statement in mutation.split("; "):
                    connection.execute(sa.text(statement))
                with pytest.raises(RuntimeError, match="incompatible"):
                    migration.upgrade()
                savepoint.rollback()
            connection.execute(
                sa.text(
                    "INSERT INTO usr_ai.t_ai_skill_spaces (id, tenant_id, created_by, name, name_key, root_folder_id, state, backend_owner) VALUES ('s','t','t','test','test','r','active','python')"
                )
            )
            with pytest.raises(RuntimeError, match="Cannot discard"):
                migration.downgrade()
            assert sa.inspect(connection).has_table("t_ai_skill_spaces", schema="usr_ai")
            connection.execute(sa.text("DELETE FROM usr_ai.t_ai_skill_spaces"))
            connection.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
            migration.downgrade()
            assert not sa.inspect(connection).has_table("t_ai_skill_spaces", schema="usr_ai")
            migration.upgrade()


@pytest.mark.asyncio
async def test_generic_files_cannot_mutate_managed_tree(bootstrapped_async_engine: Any) -> None:
    from api.db.db_models import File
    from api.skills.file_guard import is_skill_managed

    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant = uuid.uuid4().hex
    async with sessions() as db:
        service = SkillService(db, SkillStorage(MemoryObjects()), NoModels())
        space = await service.create_space(tenant, CreateSpace(name="Guard " + tenant))
        root = await db.scalar(sa.select(File).where(File.tenant_id == tenant, File.id == File.parent_id))
        child = File(id=uuid.uuid4().hex, tenant_id=tenant, created_by=tenant, parent_id=space["root_folder_id"], name="child", type="folder", source_type="skill_version", size=0, location="")
        db.add(child)
        await db.commit()
        assert await db.run_sync(lambda session: is_skill_managed(session, child.id))
        assert await db.run_sync(lambda session: is_skill_managed(session, root.id, descendants=True))
        assert not await db.run_sync(lambda session: is_skill_managed(session, root.id))
        # The public delete entry point sees the guard before any storage effect.
        from api.apps.services import file_api_service

        child_id = child.id
        ok, result = await db.run_sync(lambda session: file_api_service.delete_files(session, tenant, [child_id]))
        assert not ok and result["success_count"] == 0
        assert await db.get(File, child_id) is not None


class FakeIndex:
    def __init__(self) -> None:
        self.documents: dict[str, list[Any]] = {}
        self.fail_delete = False
        self.build_count = 0

    async def build(self, name: str, tenant: str, config: dict[str, Any], documents: list[Any]) -> int:
        self.documents[name] = list(documents)
        self.build_count += 1
        return 3

    async def delete(self, name: str, version_ids: list[str] | None = None) -> None:
        if self.fail_delete:
            raise SkillError(503, "INDEX_CLEANUP_FAILED", "Injected index cleanup failure")
        if version_ids is None:
            self.documents.pop(name, None)
        elif name in self.documents:
            self.documents[name] = [document for document in self.documents[name] if document.version_id not in version_ids]


@pytest.mark.asyncio
async def test_publish_metadata_and_cleanup_retry_do_not_republish(bootstrapped_async_engine: Any) -> None:
    from api.db.db_models import SkillSearchConfig

    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant = uuid.uuid4().hex
    index = FakeIndex()
    storage = SkillStorage(MemoryObjects())
    worker = SkillWorker(sessions, storage, index, None)
    async with sessions() as db:
        service = SkillService(db, storage, index)
        space = await service.create_space(tenant, CreateSpace(name="Publish " + tenant))
        config = await db.scalar(sa.select(SkillSearchConfig).where(SkillSearchConfig.space_id == space["id"]))
        config.embedding_model_id = 1  # The index fake replaces the external model boundary.
        await db.commit()
    results = []
    for number in (1, 2):
        async with sessions() as db:
            service = SkillService(db, storage, index)
            package = validate_package(
                UploadManifest(name="publish", version=f"{number}.0.0"), [("SKILL.md", f"---\nname: publish\ndescription: version {number}\ntags: [v{number}]\n---\nText".encode())]
            )
            result = await service.install(tenant, space["id"], package, True, f"publish-{number}")
            results.append(result)
        index.fail_delete = number == 2
        assert await worker.run_once()
    assert index.build_count == 2
    async with sessions() as db:
        service = SkillService(db, storage, index)
        operation = await service.operation(tenant, results[1]["operation_id"])
        assert operation.state == "failed"
        assert operation.payload["publication_done"] is True
        skill = await db.get(Skill, operation.payload["skill_id"])
        assert skill.description == "version 2" and skill.tags == ["v2"]
        version = await db.get(SkillVersion, operation.resource_id)
        assert version.index_state == "ready"
        active = await service.space(tenant, space["id"])
        generation = await db.get(SkillIndexGeneration, active.active_generation_id)
        assert index.documents[generation.index_name][0].description == "version 2"
        await service.retry(tenant, operation.id, "retry-cleanup")
    index.fail_delete = False
    assert await worker.run_once()
    assert index.build_count == 2
    assert len(index.documents) == 1
    async with sessions() as db:
        operation = await db.get(SkillOperation, results[1]["operation_id"])
        assert operation.state == "succeeded"


@pytest.mark.asyncio
async def test_orphan_sweep_removes_late_fenced_generation(bootstrapped_async_engine: Any) -> None:
    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant = uuid.uuid4().hex
    index = FakeIndex()
    storage = SkillStorage(MemoryObjects())
    worker = SkillWorker(sessions, storage, index, None)
    async with sessions() as db:
        service = SkillService(db, storage, index)
        space = await service.create_space(tenant, CreateSpace(name="Sweep " + tenant))
        generation_id = uuid.uuid4().hex
        index_name = "skill_" + generation_id
        db.add(SkillIndexGeneration(id=generation_id, tenant_id=tenant, space_id=space["id"], config_revision=1, source_revision=1, config={}, dimension=0, index_name=index_name, state="building"))
        operation = service.new_operation(tenant, "reindex", "old-build", {"space_id": space["id"]}, space["id"], space["id"])
        operation.payload = {"space_id": space["id"], "generations": [{"id": generation_id, "lease_revision": 1}]}
        operation.state = "failed"
        operation.revision = 2
        await db.commit()
    index.documents[index_name] = []
    await worker.sweep_orphan_generations()
    assert index_name not in index.documents
    # A thread that cannot be cancelled physically recreates its fenced output.
    index.documents[index_name] = []
    await worker.sweep_orphan_generations()
    assert index_name not in index.documents
    async with sessions() as db:
        generation = await db.get(SkillIndexGeneration, generation_id)
        assert generation.state == "deleted"


@pytest.mark.asyncio
async def test_abandoned_staging_creates_durable_cleanup_operation(bootstrapped_async_engine: Any) -> None:
    from api.db.db_models import File

    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant = uuid.uuid4().hex
    objects = MemoryObjects()
    storage = SkillStorage(objects)
    original_put = objects.put

    def interrupted_put(bucket: str, key: str, data: bytes) -> None:
        original_put(bucket, key, data)
        raise OSError("connection failed after write")

    objects.put = interrupted_put
    async with sessions() as db:
        service = SkillService(db, storage, NoModels())
        space = await service.create_space(tenant, CreateSpace(name="Staging " + tenant))
        package = validate_package(UploadManifest(name="staging", version="1.0.0"), [("SKILL.md", b"---\nname: staging\ndescription: interrupted upload\n---\nText")])
        result = await service.install(tenant, space["id"], package, False, "staging-install")
        assert result["state"] == "failed"
    assert objects.objects

    async def cleanup(uid: str, ids: list[str]) -> None:
        async with sessions() as db:
            tree = sa.select(File.id).where(File.id.in_(ids), File.tenant_id == uid).cte("staging_cleanup", recursive=True)
            tree = tree.union(sa.select(File.id).join(tree, File.parent_id == tree.c.id).where(File.tenant_id == uid))
            file_ids = list(await db.scalars(sa.select(tree.c.id)))
            await db.execute(sa.delete(File).where(File.id.in_(file_ids)))
            await db.commit()

    worker = SkillWorker(sessions, storage, NoModels(), cleanup)
    await worker.recover_staging()
    await worker.recover_staging()
    async with sessions() as db:
        operation = await db.get(SkillOperation, result["operation_id"])
        cleanup_id = operation.payload["cleanup_operation_id"]
        version_id = operation.resource_id
        assert operation.state == "failed"
        assert await db.scalar(sa.select(sa.func.count()).select_from(SkillOperation).where(SkillOperation.id == cleanup_id)) == 1
    assert await worker.run_once()
    assert not objects.objects
    async with sessions() as db:
        assert (await db.get(SkillOperation, cleanup_id)).state == "succeeded"
        assert (await db.get(SkillVersion, version_id)).state == "deleted"


@pytest.mark.asyncio
async def test_generic_file_http_hides_managed_assets_before_pagination(bootstrapped_async_engine: Any) -> None:
    import httpx
    from fastapi import FastAPI

    from api.apps.deps import get_storage
    from api.apps.restful_apis import file_api
    from api.db.db_models import File, get_async_db
    from api.utils.api_utils import async_current_tenant_id

    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant = uuid.uuid4().hex
    async with sessions() as db:
        space = await SkillService(db, SkillStorage(MemoryObjects()), NoModels()).create_space(tenant, CreateSpace(name="Hidden " + tenant))
        root = await db.scalar(sa.select(File).where(File.tenant_id == tenant, File.id == File.parent_id))
        root_id = root.id
        child_id, ordinary_id = uuid.uuid4().hex, uuid.uuid4().hex
        db.add_all(
            [
                File(id=child_id, tenant_id=tenant, created_by=tenant, parent_id=space["root_folder_id"], name="unmarked descendant", type="folder", source_type="", size=0, location=""),
                File(id=ordinary_id, tenant_id=tenant, created_by=tenant, parent_id=root_id, name="ordinary", type="file", source_type="", size=0, location=""),
            ]
        )
        await db.commit()

    async def session_dependency() -> Any:
        async with sessions() as db:
            yield db

    async def identity_dependency() -> str:
        return tenant

    app = FastAPI()
    app.include_router(file_api.router, prefix="/api/v1")
    app.dependency_overrides[get_async_db] = session_dependency
    app.dependency_overrides[async_current_tenant_id] = identity_dependency
    app.dependency_overrides[get_storage] = MemoryObjects
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/files", params={"parent_id": root_id, "page_size": 1})
        assert response.status_code == 200
        payload = response.json()
        assert payload["code"] == 0 and payload["data"]["total"] == 1
        assert [item["id"] for item in payload["data"]["files"]] == [ordinary_id]
        for identity in (space["root_folder_id"], child_id):
            for path in (f"/api/v1/files?parent_id={identity}", f"/api/v1/files/{identity}", f"/api/v1/files/{identity}/parent", f"/api/v1/files/{identity}/ancestors"):
                response = await client.get(path)
                assert response.status_code == 404, (path, response.text)


@pytest.mark.asyncio
async def test_download_rechecks_visibility_after_blocked_object_read(bootstrapped_async_engine: Any) -> None:
    import asyncio

    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant = uuid.uuid4().hex
    objects = MemoryObjects()
    storage = SkillStorage(objects)
    async with sessions() as db:
        service = SkillService(db, storage, NoModels())
        space = await service.create_space(tenant, CreateSpace(name="Slow " + tenant))
        package = validate_package(UploadManifest(name="slow", version="1.0.0"), [("SKILL.md", b"---\nname: slow\ndescription: slow read\n---\nPrivate package")])
        installed = await service.install(tenant, space["id"], package, False, "slow-install")
        version_id = installed["resource_id"]
    assert await SkillWorker(sessions, storage, NoModels(), None).run_once()
    entered, released = asyncio.Event(), asyncio.Event()
    real_get = storage.get

    async def blocked_get(bucket: str, key: str, digest: str, size: int) -> bytes:
        content = await real_get(bucket, key, digest, size)
        entered.set()
        await released.wait()
        return content

    storage.get = blocked_get
    async with sessions() as db:
        pending = asyncio.create_task(SkillService(db, storage, NoModels()).content(tenant, space["id"], version_id))
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with sessions() as writer:
            await SkillService(writer, storage, NoModels()).register_action(tenant, space["id"], "delete_version", "slow-delete", version_id)
        released.set()
        with pytest.raises(SkillError) as hidden:
            await pending
        assert hidden.value.status == 404


@pytest.mark.asyncio
async def test_install_alias_binds_request_and_serializes_competing_bodies(bootstrapped_async_engine: Any) -> None:
    import asyncio

    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant = uuid.uuid4().hex
    storage = SkillStorage(MemoryObjects())
    files = [("SKILL.md", b"---\nname: alias\ndescription: durable key\n---\nContents")]
    first = validate_package(UploadManifest(name="alias", version="1.0.0"), files)
    second = validate_package(UploadManifest(name="alias", version="2.0.0"), files)
    async with sessions() as db:
        service = SkillService(db, storage, NoModels())
        space = await service.create_space(tenant, CreateSpace(name="Aliases " + tenant))
        installed = await service.install(tenant, space["id"], first, False, "original")
        duplicate = await service.install(tenant, space["id"], first, False, "alias-one")
        assert duplicate["operation_id"] == installed["operation_id"]
        with pytest.raises(SkillError) as conflict:
            await service.install(tenant, space["id"], second, False, "alias-one")
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
        await db.rollback()
        operation = await db.get(SkillOperation, installed["operation_id"])
        assert operation.payload["idempotency_aliases"]["alias-one"] == operation.request_hash
        assert operation.payload["objects"] and operation.payload["description"] == "durable key"

    async def submit(package: Any, key: str) -> dict[str, Any] | SkillError:
        async with sessions() as db:
            try:
                return await SkillService(db, storage, NoModels()).install(tenant, space["id"], package, False, key)
            except SkillError as exc:
                return exc

    results = await asyncio.wait_for(asyncio.gather(submit(first, "racing-key"), submit(second, "racing-key")), timeout=10)
    successes = [result for result in results if isinstance(result, dict)]
    errors = [result for result in results if isinstance(result, SkillError)]
    assert len(successes) == len(errors) == 1
    assert errors[0].status == 409 and errors[0].code == "IDEMPOTENCY_CONFLICT"
    same = await asyncio.wait_for(asyncio.gather(submit(first, "same-key"), submit(first, "same-key")), timeout=10)
    assert all(isinstance(result, dict) and result["operation_id"] == installed["operation_id"] for result in same)
    async with sessions() as db:
        operation = await db.get(SkillOperation, installed["operation_id"])
        assert operation.payload["idempotency_aliases"]["alias-one"] == operation.request_hash
        assert operation.payload["idempotency_aliases"]["same-key"] == operation.request_hash


@pytest.mark.asyncio
async def test_uninstall_skips_deleted_versions_and_retries_then_deletes_parent(bootstrapped_async_engine: Any) -> None:
    from api.db.db_models import File, SkillSearchConfig, SkillVersionFile

    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant = uuid.uuid4().hex
    objects = MemoryObjects()
    storage = SkillStorage(objects)
    index = FakeIndex()
    fail_cleanup = False

    async def cleanup(uid: str, ids: list[str]) -> None:
        if fail_cleanup:
            # Unknown adapter/SQLAlchemy errors must not become public error codes.
            raise sa.exc.IntegrityError("private SQL", {}, RuntimeError("private driver error"), code="gkpj")
        async with sessions() as db:
            tree = sa.select(File.id).where(File.id.in_(ids), File.tenant_id == uid).cte("uninstall_tree", recursive=True)
            tree = tree.union(sa.select(File.id).join(tree, File.parent_id == tree.c.id).where(File.tenant_id == uid))
            await db.execute(sa.delete(File).where(File.id.in_(sa.select(tree.c.id))))
            await db.commit()

    worker = SkillWorker(sessions, storage, index, cleanup)
    async with sessions() as db:
        service = SkillService(db, storage, index)
        space = await service.create_space(tenant, CreateSpace(name="Uninstall " + tenant))
        config = await db.scalar(sa.select(SkillSearchConfig).where(SkillSearchConfig.space_id == space["id"]))
        config.embedding_model_id = 1
        await db.commit()
    versions = []
    for number in (1, 2):
        async with sessions() as db:
            service = SkillService(db, storage, index)
            package = validate_package(UploadManifest(name="uninstall", version=f"{number}.0.0"), [("SKILL.md", f"---\nname: uninstall\ndescription: version {number}\n---\nBody".encode())])
            result = await service.install(tenant, space["id"], package, True, f"uninstall-v{number}")
            versions.append(result["resource_id"])
        assert await worker.run_once()
    async with sessions() as db:
        service = SkillService(db, storage, index)
        version = await db.get(SkillVersion, versions[0])
        skill_id = version.skill_id
        await service.register_action(tenant, space["id"], "delete_version", "remove-v1", versions[0])
    assert await worker.run_once()
    async with sessions() as db:
        first = await db.get(SkillVersion, versions[0])
        assert first.state == "deleted"
        first_deleted_at = first.deleted_at
        removed = await SkillService(db, storage, index).register_batch(tenant, space["id"], [skill_id], "uninstall-skill")
    fail_cleanup = True
    assert await worker.run_once()
    async with sessions() as db:
        operation = await db.get(SkillOperation, removed["operation_id"])
        assert operation.state == "failed"
        assert operation.result["items"][0]["error_code"] == "DELETE_FAILED"
        assert (await db.get(SkillVersion, versions[0])).deleted_at == first_deleted_at
        await SkillService(db, storage, index).retry(tenant, operation.id, "retry-uninstall")
    fail_cleanup = False
    assert await worker.run_once()
    async with sessions() as db:
        operation = await db.get(SkillOperation, removed["operation_id"])
        assert operation.state == "succeeded", operation.result
        skill = await db.get(Skill, skill_id)
        assert skill.state == "deleted"
        skill_deleted_at = skill.deleted_at
        assert (await db.get(SkillVersion, versions[0])).deleted_at == first_deleted_at
        parent = await SkillService(db, storage, index).register_batch(tenant, None, [space["id"]], "delete-parent")
    assert await worker.run_once()
    async with sessions() as db:
        operation = await db.get(SkillOperation, parent["operation_id"])
        assert operation.state == "succeeded", operation.result
        assert (await db.get(SkillSpace, space["id"])).state == "deleted"
        assert (await db.get(Skill, skill_id)).deleted_at == skill_deleted_at
        assert (await db.get(SkillVersion, versions[0])).deleted_at == first_deleted_at
        assert await db.scalar(sa.select(sa.func.count()).select_from(SkillVersionFile).where(SkillVersionFile.tenant_id == tenant)) == 0
        assert await db.scalar(sa.select(sa.func.count()).select_from(File).where(File.tenant_id == tenant, File.source_type.in_(["skill_space", "skill", "skill_version", "skill_file"]))) == 0
    assert objects.objects == {} and index.documents == {}


@pytest.mark.asyncio
async def test_create_space_ignores_managed_self_parent_root(bootstrapped_async_engine: Any) -> None:
    from api.db.db_models import File

    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant = uuid.uuid4().hex
    broken_id = uuid.uuid4().hex
    async with sessions() as db:
        db.add(File(id=broken_id, tenant_id=tenant, created_by=tenant, parent_id=broken_id, name="legacy skill root", type="folder", source_type="skill_space", size=0, location=""))
        await db.commit()
        service = SkillService(db, SkillStorage(MemoryObjects()), NoModels())
        space = await service.create_space(tenant, CreateSpace(name="Valid root " + tenant))
        managed_root = await db.get(File, space["root_folder_id"])
        ordinary_root = await db.get(File, managed_root.parent_id)
        assert ordinary_root.id != broken_id
        assert ordinary_root.parent_id == ordinary_root.id and ordinary_root.source_type == ""
        from api.db.services.file_service import FileService

        public_root = await db.run_sync(lambda session: FileService.get_root_folder(session, tenant))
        assert public_root["id"] == ordinary_root.id


@pytest.mark.asyncio
async def test_generic_file_listing_preserves_legacy_null_source(bootstrapped_async_engine: Any) -> None:
    from api.db.db_models import File
    from api.db.services.file_service import FileService

    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant, identity = uuid.uuid4().hex, uuid.uuid4().hex
    async with sessions() as db:
        # Simulate the nullable historical schema only inside this rolled-back
        # scratch transaction; current new databases enforce NOT NULL.
        await db.execute(sa.text("ALTER TABLE usr_ai.t_ai_files ALTER COLUMN source_type DROP NOT NULL"))
        db.add(File(id=identity, tenant_id=tenant, created_by=tenant, parent_id=tenant, name="legacy.txt", type="file", size=7, source_type="", location=""))
        await db.flush()
        await db.execute(sa.update(File).where(File.id == identity).values(source_type=None))
        files, count = await db.run_sync(lambda session: FileService.get_by_pf_id(session, tenant, tenant, 1, 1, "name", False))
        assert count == 1 and [file["id"] for file in files] == [identity]
        assert await db.run_sync(lambda session: FileService.get_folder_size(session, tenant)) == 7
        await db.rollback()
