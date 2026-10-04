"""Fault acceptance for native directory state; every database is scratch."""

import time
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.db.db_models import File, PythonSkillCoreSpace
from api.skills.core_runtime import sweep
from api.skills.core_schemas import CoreCreate
from api.skills.core_service import SkillCoreService
from api.skills.schemas import SkillError
from api.skills.search import SkillSearchRuntime
from api.skills.storage import SkillStorage
from tests.support.skill_objects import MemoryObjects


class Indices(SkillSearchRuntime):
    def __init__(self) -> None:
        self.fail = False
        self.deleted: list[str] = []

    async def delete(self, name: str, version_ids: list[str] | None = None) -> None:
        if self.fail:
            raise RuntimeError("injected backend fault")
        self.deleted.append(name)


@pytest.mark.asyncio
async def test_core_failed_delete_retains_plan_hides_and_retries(bootstrapped_async_engine: Any) -> None:
    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant, objects, index = uuid4().hex, MemoryObjects(), Indices()
    storage = SkillStorage(objects)
    async with sessions() as db:
        svc = SkillCoreService(db, storage, index)
        space = await svc.create(tenant, CoreCreate(name="fault"))
        files = await svc.upload(tenant, space["folder_id"], [(b"bytes", "nested/file.txt")])
        file_id = files[0]["id"]
        await svc.plan_delete(tenant, space["id"], space["folder_id"], entire=True)
        original_rm = objects.rm
        objects.rm = lambda bucket, key: None
        with pytest.raises(SkillError, match="still exists"):
            await svc.clean(tenant, space["id"])
        row = await svc.space(tenant, space["id"], active=False)
        assert row.state == "delete_failed" and row.cleanup["files"][0]["objects"]
        with pytest.raises(SkillError) as hidden:
            await svc.file_space(tenant, file_id)
        assert hidden.value.status == 404
        objects.rm = original_rm
        await svc.clean(tenant, space["id"])
        assert not objects.objects
        assert await db.scalar(select(File.id).where(File.id == file_id)) is None
        assert (await db.get(PythonSkillCoreSpace, space["id"])).state == "deleted"


@pytest.mark.asyncio
async def test_core_abandoned_upload_sweep_and_missing_index_are_real(bootstrapped_async_engine: Any) -> None:
    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant, objects, index = uuid4().hex, MemoryObjects(), Indices()
    async with sessions() as db:
        svc = SkillCoreService(db, SkillStorage(objects), index)
        space = await svc.create(tenant, CoreCreate(name="abandoned"))
        original_put = objects.put

        def broken_put(bucket: str, key: str, data: bytes) -> None:
            original_put(bucket, key, data)
            raise RuntimeError("after object write")

        objects.put = broken_put
        with pytest.raises(RuntimeError, match="after object"):
            await svc.upload(tenant, space["folder_id"], [(b"bytes", "abandoned.txt")])
        row = await svc.space(tenant, space["id"])
        assert row.cleanup["uploads"]["ids"]
        ids = row.cleanup["uploads"]["ids"]
        row.cleanup = {**row.cleanup, "uploads": {**row.cleanup["uploads"], "deadline": time.time() - 1}}
        await db.commit()
    await sweep(sessions, SkillStorage(objects), index)
    async with sessions() as db:
        assert not objects.objects
        assert list(await db.scalars(select(File.id).where(File.id.in_(ids)))) == []
        assert "files" not in (await db.get(PythonSkillCoreSpace, space["id"])).cleanup


@pytest.mark.asyncio
async def test_core_migration_rejects_drift_and_nonempty_downgrade(pg_scratch_engine: Any) -> None:
    import importlib.util
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = importlib.util.spec_from_file_location("python_core_migration", Path(__file__).parents[2] / "configs/alembic/versions/d6f8a0b2c4e6_add_python_skill_core.py")
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with pg_scratch_engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE IF EXISTS usr_ai.t_ai_python_skill_search_configs CASCADE"))
        connection.execute(sa.text("DROP TABLE IF EXISTS usr_ai.t_ai_python_skill_spaces CASCADE"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
            for statement in (
                "ALTER TABLE usr_ai.t_ai_python_skill_spaces DROP CONSTRAINT ck_python_skill_core_state",
                "ALTER TABLE usr_ai.t_ai_python_skill_search_configs DROP CONSTRAINT fk_python_skill_core_config",
                "DROP INDEX usr_ai.uq_python_skill_core_name",
                "ALTER TABLE usr_ai.t_ai_python_skill_search_configs DROP CONSTRAINT uq_python_skill_core_config",
            ):
                savepoint = connection.begin_nested()
                connection.execute(sa.text(statement))
                with pytest.raises(RuntimeError, match="incompatible"):
                    migration.upgrade()
                savepoint.rollback()
            connection.execute(sa.text("INSERT INTO usr_ai.t_ai_python_skill_spaces (id,tenant_id,name,folder_id) VALUES ('s','t','n','f')"))
            with pytest.raises(RuntimeError, match="Cannot discard"):
                migration.downgrade()
            connection.execute(sa.text("DELETE FROM usr_ai.t_ai_python_skill_spaces"))
            migration.downgrade()
            assert not sa.inspect(connection).has_table("t_ai_python_skill_spaces", schema="usr_ai")
            migration.upgrade()


@pytest.mark.asyncio
async def test_late_upload_cannot_publish_or_leave_bytes_after_recovery(bootstrapped_async_engine: Any) -> None:
    import asyncio
    import threading

    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant, objects, index = uuid4().hex, MemoryObjects(), Indices()
    entered, release = threading.Event(), threading.Event()
    original = objects.put

    def delayed_put(bucket: str, key: str, data: bytes) -> None:
        entered.set()
        assert release.wait(10)
        original(bucket, key, data)

    objects.put = delayed_put
    async with sessions() as db:
        svc = SkillCoreService(db, SkillStorage(objects), index)
        space = await svc.create(tenant, CoreCreate(name="late upload"))
    async with sessions() as writer:
        pending = asyncio.create_task(SkillCoreService(writer, SkillStorage(objects), index).upload(tenant, space["folder_id"], [(b"late", "nested/late.txt")]))
        assert await asyncio.to_thread(entered.wait, 5)
        async with sessions() as db:
            row = await db.get(PythonSkillCoreSpace, space["id"])
            upload = dict(row.cleanup["uploads"])
            row.cleanup = {**row.cleanup, "uploads": {**upload, "deadline": 0}}
            await db.commit()
        await sweep(sessions, SkillStorage(objects), index)
        release.set()
        with pytest.raises(SkillError) as failure:
            await pending
        assert failure.value.code == "CONCURRENT_CHANGE"
    assert objects.objects  # The old storage call really returned after cleanup.
    await sweep(sessions, SkillStorage(objects), index)
    assert not objects.objects
    async with sessions() as db:
        assert list(await db.scalars(select(File.id).where(File.id.in_(upload["ids"])))) == []


@pytest.mark.asyncio
async def test_sql_delete_failure_after_objects_removed_is_retryable(bootstrapped_async_engine: Any) -> None:
    from sqlalchemy import text

    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant, objects, index = uuid4().hex, MemoryObjects(), Indices()
    async with sessions() as db:
        svc = SkillCoreService(db, SkillStorage(objects), index)
        space = await svc.create(tenant, CoreCreate(name="SQL failure"))
        files = await svc.upload(tenant, space["folder_id"], [(b"real bytes", "file.txt")])
        identity = files[0]["id"]
        await svc.plan_delete(tenant, space["id"], identity)
        await db.execute(
            text(f"CREATE FUNCTION usr_ai.core_delete_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF OLD.id = '{identity}' THEN RAISE EXCEPTION 'injected'; END IF; RETURN OLD; END $$")
        )
        await db.execute(text("CREATE TRIGGER core_delete_fault BEFORE DELETE ON usr_ai.t_ai_files FOR EACH ROW EXECUTE FUNCTION usr_ai.core_delete_fault()"))
        await db.commit()
        try:
            with pytest.raises(Exception, match="injected"):
                await svc.clean(tenant, space["id"])
            assert not objects.objects
            assert await db.get(File, identity) is not None
            assert (await svc.space(tenant, space["id"])).cleanup["files"]
        finally:
            await db.rollback()
            await db.execute(text("DROP TRIGGER core_delete_fault ON usr_ai.t_ai_files"))
            await db.execute(text("DROP FUNCTION usr_ai.core_delete_fault()"))
            await db.commit()
        await svc.clean(tenant, space["id"])
        assert await db.get(File, identity) is None


@pytest.mark.asyncio
async def test_core_late_build_is_not_published_and_reclaimed(bootstrapped_async_engine: Any) -> None:
    import asyncio

    from api.db.db_models import PythonSkillCoreConfig
    from api.skills.core_schemas import CoreIndexRequest

    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant, objects = uuid4().hex, MemoryObjects()
    entered, release = asyncio.Event(), asyncio.Event()
    physical: set[str] = set()

    class DelayedIndex(Indices):
        async def build(self, name: str, tenant: str, config: dict[str, Any], documents: Any) -> int:
            entered.set()
            await release.wait()
            physical.add(name)
            return 2

        async def delete(self, name: str, version_ids: list[str] | None = None) -> None:
            physical.discard(name)

    index = DelayedIndex()
    async with sessions() as db:
        svc = SkillCoreService(db, SkillStorage(objects), index)
        space = await svc.create(tenant, CoreCreate(name="late build"))
        skill = await svc.create_folder(tenant, space["folder_id"], "legacy")
        await svc.upload(tenant, skill["id"], [(b"---\nname: legacy\ndescription: stable\n---\ncontent", "SKILL.md")])
        config = await svc.config_row(tenant, space["id"])
        config.settings = {**config.settings, "embd_id": "17"}
        await db.commit()
    async with sessions() as writer:
        pending = asyncio.create_task(SkillCoreService(writer, SkillStorage(objects), index).index(tenant, CoreIndexRequest(space_id=space["id"], skills=[]), rebuild=True))
        await asyncio.wait_for(entered.wait(), 5)
        async with sessions() as db:
            config = await db.scalar(select(PythonSkillCoreConfig).where(PythonSkillCoreConfig.space_id == space["id"]))
            config.index_data = {**config.index_data, "retired": [{**item, "after": 0} for item in config.index_data["retired"]]}
            await db.commit()
        await sweep(sessions, SkillStorage(objects), index)
        release.set()
        with pytest.raises(SkillError) as failure:
            await pending
        assert failure.value.code == "CONCURRENT_CHANGE"
    assert physical
    await sweep(sessions, SkillStorage(objects), index)
    assert not physical
    async with sessions() as db:
        config = await db.scalar(select(PythonSkillCoreConfig).where(PythonSkillCoreConfig.space_id == space["id"]))
        assert "name" not in config.index_data
