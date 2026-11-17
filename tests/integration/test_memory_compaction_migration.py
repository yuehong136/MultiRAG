"""Only owned scratch tables are exercised by context-artifact migrations."""

import importlib.util
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import sqlalchemy as sa
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
