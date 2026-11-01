"""Fresh and historical SQL schemas permit distinct instances with empty keys."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from api.db.db_models import TenantModel, TenantModelInstance, TenantModelProvider
from tests.support.database import scratch_database
from tests.support.resource_journal import ResourceJournal


@pytest.mark.parametrize("install", ["fresh", "upgrade"])
def test_empty_keys_keep_provider_instance_and_tenant_uniqueness(tmp_path: Path, install: str) -> None:
    with scratch_database(tmp_path) as engine:
        journal = ResourceJournal("after325-empty-instance-keys-" + install)
        ids = {key: uuid4().hex for key in ["owner", "other", "provider", "other_provider", "first", "second", "other_instance"]}
        journal.event("reserve-sql", database=engine.url.database, ids=ids, install=install)
        if install == "upgrade":
            path = Path(__file__).resolve().parents[2] / "configs/alembic/versions/b8d0f2a4c6e8_add_model_provider_instances.py"
            spec = importlib.util.spec_from_file_location("after325_instance_migration", path)
            assert spec and spec.loader
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            journal.event("reserve-scratch-schema-upgrade", database=engine.url.database, tables=[table.__tablename__ for table in [TenantModel, TenantModelInstance, TenantModelProvider]])
            with engine.begin() as connection:
                for model in [TenantModel, TenantModelInstance, TenantModelProvider]:
                    model.__table__.drop(connection)
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                    migration.upgrade()
        inspector = sa.inspect(engine)
        constraints = inspector.get_unique_constraints(TenantModelInstance.__tablename__, schema="usr_ai")
        assert {tuple(item["column_names"]) for item in constraints} == {("provider_id", "instance_name")}
        assert not any(index["unique"] and "api_key" in index["column_names"] for index in inspector.get_indexes(TenantModelInstance.__tablename__, schema="usr_ai"))
        with Session(engine) as db:
            for provider_key, owner_key in [("provider", "owner"), ("other_provider", "other")]:
                db.add(TenantModelProvider(id=ids[provider_key], tenant_id=ids[owner_key], provider_name="Ollama"))
            db.flush()
            for key, provider_key, name in [("first", "provider", "local-a"), ("second", "provider", "local-b"), ("other_instance", "other_provider", "local-a")]:
                db.add(TenantModelInstance(id=ids[key], provider_id=ids[provider_key], instance_name=name, api_key="", base_url="http://127.0.0.1:11434"))
            db.commit()
        for model, fields, constraint in [
            (TenantModelInstance, {"id": uuid4().hex, "provider_id": ids["provider"], "instance_name": "local-a", "api_key": ""}, "uq_model_instance_provider_name"),
            (TenantModelProvider, {"id": uuid4().hex, "tenant_id": ids["owner"], "provider_name": "Ollama"}, "uq_model_provider_owner_name"),
        ]:
            journal.event("reserve-rejected-sql", database=engine.url.database, model=model.__tablename__, id=fields["id"])
            with Session(engine) as db, pytest.raises(sa.exc.IntegrityError, match=constraint):
                db.add(model(**fields))
                db.commit()
        with engine.connect() as reader:
            rows = list(reader.execute(sa.select(TenantModelInstance.id, TenantModelInstance.api_key, TenantModelInstance.provider_id).order_by(TenantModelInstance.id)))
            assert {row.id for row in rows} == {ids[key] for key in ["first", "second", "other_instance"]}
            assert all(row.api_key == "" for row in rows)
            assert reader.scalar(sa.select(sa.func.count()).select_from(TenantModelProvider)) == 2
        journal.event(
            "instance-constraint-independent-readback",
            empty_key_instance_count=3,
            provider_count=2,
            preserved_constraints=["uq_model_instance_provider_name", "uq_model_provider_owner_name"],
            install=install,
        )
