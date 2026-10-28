"""Transactional numeric default repair, including late legacy OCR imports."""

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from api.db.db_models import Base, Tenant, TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider
from api.db.model_backfill import backfill, backfill_tenant_defaults
from api.db.model_contract import DEFAULT_MODEL_FIELDS
from tests.support.database import _alembic_config
from tests.support.resource_journal import ResourceJournal

pytestmark = pytest.mark.integration


@pytest.fixture
def defaults_engine(pg_scratch_engine: Engine) -> Iterator[Engine]:
    journal = ResourceJournal("model-defaults-sql")

    def reserve(_connection: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: bool) -> None:
        journal.event("reserve-sql", database=pg_scratch_engine.url.database, statement_kind=statement.lstrip().split(maxsplit=1)[0])

    event.listen(pg_scratch_engine, "before_cursor_execute", reserve)
    tables = [
        Base.metadata.tables[f"usr_ai.{name}"]
        for name in ("t_ai_llm_factories", "t_ai_llms", "t_ai_tenants", "t_ai_tenant_llms", "t_ai_tenant_model_providers", "t_ai_tenant_model_instances", "t_ai_tenant_models")
    ]
    Base.metadata.create_all(pg_scratch_engine, tables=tables)
    try:
        yield pg_scratch_engine
    finally:
        event.remove(pg_scratch_engine, "before_cursor_execute", reserve)


def add_tenant(db: Session, **values: Any) -> Tenant:
    row = Tenant(id=uuid4().hex, name="Default fixture", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="", **values)
    db.add(row)
    db.flush()
    return row


def add_model(db: Session, owner: str, capability: str, name: str = "fixture", factory: str = "OpenAI") -> int:
    row = TenantLLM(tenant_id=owner, llm_factory=factory, llm_name=name, mdl_type=capability, api_key="fixture-only", api_base="", max_tokens=0, status="1")
    db.add(row)
    db.flush()
    return row.id


def test_all_capabilities_and_late_ocr_are_repaired_without_rewriting_aliases(defaults_engine: Engine) -> None:
    with Session(defaults_engine) as db:
        owner = add_tenant(db)
        identifiers = {}
        for capability, field in DEFAULT_MODEL_FIELDS.items():
            identifiers[field] = add_model(db, owner.id, capability, capability)
            setattr(owner, field, f"{capability}@OpenAI")
        owner_id = owner.id
        db.commit()
        with defaults_engine.begin() as connection:
            backfill(connection)
        assert backfill_tenant_defaults(db) == 7
        db.commit()
    with Session(defaults_engine) as reader:
        stored = reader.get(Tenant, owner_id)
        assert stored is not None
        for field, identifier in identifiers.items():
            assert getattr(stored, f"tenant_{field}") == identifier
            assert getattr(stored, field) == f"{next(key for key, value in DEFAULT_MODEL_FIELDS.items() if value == field)}@OpenAI"
        assert backfill_tenant_defaults(reader) == 0
        late = add_tenant(reader, ocr_id="ocr@OpenAI")
        late_id = late.id
        late_model = add_model(reader, late_id, "ocr", "ocr")
        reader.commit()
        assert backfill_tenant_defaults(reader) == 1
        reader.commit()
    with Session(defaults_engine) as reader:
        assert reader.get(Tenant, late_id).tenant_ocr_id == late_model


def test_existing_numeric_choice_and_foreign_or_missing_names_are_preserved(defaults_engine: Engine) -> None:
    with Session(defaults_engine) as db:
        owner = add_tenant(db, ocr_id="foreign@OpenAI", tenant_ocr_id=None)
        foreign = add_tenant(db)
        add_model(db, foreign.id, "ocr", "foreign")
        chosen = add_model(db, owner.id, "chat", "chosen")
        owner.llm_id = "unresolved@OpenAI"
        owner.tenant_llm_id = chosen
        owner_id = owner.id
        db.commit()
        assert backfill_tenant_defaults(db) == 0
        db.commit()
    with Session(defaults_engine) as reader:
        stored = reader.get(Tenant, owner_id)
        assert stored.tenant_llm_id == chosen and stored.tenant_ocr_id is None
        assert stored.llm_id == "unresolved@OpenAI"


def test_ambiguous_default_rolls_back_earlier_repairs(defaults_engine: Engine) -> None:
    with Session(defaults_engine) as db:
        owner = add_tenant(db, ocr_id="ambiguous")
        owner.llm_id = "chat@OpenAI"
        add_model(db, owner.id, "chat", "chat")
        add_model(db, owner.id, "ocr", "ambiguous", "OpenAI")
        add_model(db, owner.id, "ocr", "ambiguous", "Other")
        owner_id = owner.id
        db.commit()
        with pytest.raises(LookupError, match="ambiguous"):
            backfill_tenant_defaults(db)
        db.rollback()
    with Session(defaults_engine) as reader:
        stored = reader.get(Tenant, owner_id)
        assert stored.tenant_llm_id is None and stored.tenant_ocr_id is None
        assert len(list(reader.scalars(select(TenantLLM).where(TenantLLM.tenant_id == owner_id)))) == 3


def test_alembic_version_and_backfill_commit_or_rollback_together(defaults_engine: Engine) -> None:
    config = _alembic_config()
    with Session(defaults_engine) as db:
        owner = add_tenant(db)
        owner_id = owner.id
        invalid = add_model(db, owner_id, "chat", "")
        db.commit()
    with defaults_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.stamp(config, "a7c9e1f3b5d7")
    with pytest.raises(RuntimeError, match="invalid name or capability"):
        with defaults_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "b8d0f2a4c6e8")
    with Session(defaults_engine) as reader:
        assert reader.scalar(text("SELECT version_num FROM usr_ai.alembic_version")) == "a7c9e1f3b5d7"
        assert reader.scalar(select(func.count()).select_from(TenantModelProvider).where(TenantModelProvider.tenant_id == owner_id)) == 0
        assert reader.scalar(select(func.count()).select_from(TenantModelInstance).join(TenantModelProvider).where(TenantModelProvider.tenant_id == owner_id)) == 0
        assert reader.get(TenantModel, invalid) is None
        reader.get(TenantLLM, invalid).llm_name = "repaired"
        reader.commit()
    with defaults_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "b8d0f2a4c6e8")
    with Session(defaults_engine) as reader:
        assert reader.scalar(text("SELECT version_num FROM usr_ai.alembic_version")) == "b8d0f2a4c6e8"
        assert reader.get(TenantModel, invalid).model_name == "repaired"
        # A repeated startup does not replay an already committed revision.
        late = add_model(reader, owner_id, "ocr", "late-ocr")
        reader.commit()
    with defaults_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "b8d0f2a4c6e8")
    with Session(defaults_engine) as reader:
        assert reader.get(TenantModel, late) is None
    # New user/import rows are handled by the explicit idempotent data path.
    with defaults_engine.begin() as connection:
        backfill(connection, owner_id)
    with Session(defaults_engine) as reader:
        assert reader.get(TenantModel, late).model_name == "late-ocr"
