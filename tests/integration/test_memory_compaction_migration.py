"""Only owned scratch tables are exercised by context-artifact migrations."""

import importlib.util
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import op
from alembic.migration import MigrationContext
from alembic.operations import Operations

from api.db.memory_compaction_models import MEMORY_COMPACTION_MODELS, MemoryCompactionArtifact
from tests.support.resource_journal import ResourceJournal


def test_compaction_migration_is_idempotent_and_nonempty_audit_cannot_downgrade(bootstrapped_engine: sa.Engine) -> None:
    engine = bootstrapped_engine
    assert str(engine.url.database).startswith("multirag_test_")
    journal = ResourceJournal("memory-compaction-migration")
    path = Path(__file__).parents[2] / "configs/alembic/versions/fc24e6f8b0d2_add_memory_context_artifacts.py"
    spec = importlib.util.spec_from_file_location("owned_compaction_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal.event("reserve-owned-migration-tables", database=engine.url.database, tables=[model.__tablename__ for model in MEMORY_COMPACTION_MODELS])
    with engine.begin() as connection:
        for model in reversed(MEMORY_COMPACTION_MODELS):
            cast(sa.Table, model.__table__).drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            module.upgrade()
    identity = uuid4().hex
    journal.event("reserve-migration-audit-row", database=engine.url.database, artifact_id=identity)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.insert(MemoryCompactionArtifact).values(
                    artifact_id=identity,
                    memory_id="owned-migration",
                    tenant_id="owned",
                    actor_id="owned",
                    operation_id="owned-migration",
                    request_sha256="0" * 64,
                    request={},
                    state="failed",
                    input_manifest_sha256="0" * 64,
                    model_binding={},
                    usage={},
                )
            )
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="Cannot discard"):
            module.downgrade()
    finally:
        with engine.begin() as connection:
            connection.execute(sa.delete(MemoryCompactionArtifact).where(MemoryCompactionArtifact.artifact_id == identity))
        with engine.connect() as reader:
            assert reader.scalar(sa.select(sa.func.count()).select_from(MemoryCompactionArtifact)) == 0
        journal.event("compaction-migration-cleanup-readback", audit_rows=0)


@pytest.mark.parametrize("restore_check", [False, True], ids=["native-check", "restored-check"])
def test_retirement_migration_preserves_existing_generation_row_and_guards_downgrade(bootstrapped_engine: sa.Engine, restore_check: bool) -> None:
    engine = bootstrapped_engine
    assert str(engine.url.database).startswith("multirag_test_")
    path = Path(__file__).parents[2] / "configs/alembic/versions/0d35f7a9c1e3_add_compaction_retirement.py"
    spec = importlib.util.spec_from_file_location("owned_retirement_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal = ResourceJournal("memory-compaction-retirement-migration")
    artifact_id = uuid4().hex
    table = cast(sa.Table, MemoryCompactionArtifact.__table__)
    journal.event("reserve-owned-migration-tables", database=engine.url.database, tables=[table.name], artifact_id=artifact_id)
    package = {"summary": "owned legacy context"}
    with engine.begin() as connection:
        connection.execute(
            sa.insert(table).values(
                artifact_id=artifact_id,
                memory_id="owned-migration",
                tenant_id="owned",
                actor_id="owned",
                operation_id="owned-retirement-migration",
                request_sha256="1" * 64,
                request={"source_ids": ["100"]},
                state="generated",
                input_manifest_sha256="2" * 64,
                model_binding={},
                package=package,
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
        old_columns = {column["name"] for column in sa.inspect(connection).get_columns(table.name, schema="usr_ai")}
        assert {"retired_at", "retirement"}.isdisjoint(old_columns)
        if restore_check:
            definition = connection.execute(
                sa.text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = to_regclass(:table) AND conname = 'ck_memory_compaction_state'"),
                {"table": table.fullname},
            ).scalar_one()
            with Operations.context(MigrationContext.configure(connection)):
                op.drop_constraint("ck_memory_compaction_state", table.name, type_="check", schema="usr_ai")
            connection.exec_driver_sql(f"ALTER TABLE {table.fullname} ADD CONSTRAINT ck_memory_compaction_state {definition}")
        with connection.begin_nested() as invalid, Operations.context(MigrationContext.configure(connection)):
            op.drop_constraint("ck_memory_compaction_state", table.name, type_="check", schema="usr_ai")
            op.create_check_constraint("ck_memory_compaction_state", table.name, "state IN ('generated','unsupported')", schema="usr_ai")
            with pytest.raises(RuntimeError, match="incompatible check constraint: ck_memory_compaction_state"):
                module.upgrade()
            invalid.rollback()
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            module.upgrade()
        row = connection.execute(sa.select(table).where(table.c.artifact_id == artifact_id)).mappings().one()
        assert row["request_sha256"] == "1" * 64 and row["package"] == package and row["retirement"] is None and row["retired_at"] is None
        connection.execute(
            sa.update(table)
            .where(table.c.artifact_id == artifact_id)
            .values(state="stale", request={}, package=None, retirement={"preview_sha256": "3" * 64}, retired_at=sa.func.now(), redacted=True, cancellation_requested=True)
        )
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="Cannot discard compaction retirement tombstones"):
            module.downgrade()
    finally:
        with engine.begin() as connection:
            connection.execute(sa.delete(table).where(table.c.artifact_id == artifact_id))
        with engine.connect() as reader:
            assert not reader.scalar(sa.select(sa.exists().where(table.c.artifact_id == artifact_id)))
        journal.event("compaction-retirement-migration-cleanup-readback", audit_row_absent=True)
