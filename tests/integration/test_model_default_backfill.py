"""Stored default conversion, historical linking, and cutover concurrency in PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

import pytest
from alembic import command
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from tests.support.database import _alembic_config
from tests.support.model_identity_migration import legacy_database as legacy_database
from tests.support.model_identity_migration import migrated_model_id, upgrade_model_identity

pytestmark = pytest.mark.integration
_CAPABILITIES = {"chat": "llm_id", "embedding": "embd_id", "speech2text": "asr_id", "image2text": "img2txt_id", "rerank": "rerank_id", "tts": "tts_id", "ocr": "ocr_id"}


def test_all_defaults_are_linked_with_explicit_id_taking_precedence(legacy_database: Engine) -> None:
    registrations = {}
    with legacy_database.begin() as db:
        for identifier, (capability, field) in enumerate(_CAPABILITIES.items(), 70):
            registrations[field] = identifier
            db.execute(
                text("INSERT INTO usr_ai.t_ai_tenant_llms VALUES (:id, 'all-defaults', 'OpenAI', :name, :kind, 'fixture-key', '', 0, 37, '1')"),
                {"id": identifier, "name": capability, "kind": capability},
            )
        db.execute(
            text(
                "INSERT INTO usr_ai.t_ai_tenants (id, llm_id, tenant_llm_id, embd_id, asr_id, img2txt_id, rerank_id, tts_id, ocr_id) "
                "VALUES ('all-defaults', 'old-stale-display', 70, 'embedding@OpenAI', 'speech2text@OpenAI', "
                "'image2text@OpenAI', 'rerank@OpenAI', 'tts@OpenAI', 'ocr@OpenAI')"
            )
        )
        db.execute(text("INSERT INTO usr_ai.t_ai_tenants (id, ocr_id) VALUES ('other-owner', 'ocr@OpenAI')"))
    upgrade_model_identity(legacy_database)

    with legacy_database.connect() as db:
        stored = db.execute(text("SELECT * FROM usr_ai.t_ai_tenants WHERE id = 'all-defaults'")).mappings().one()
        for field, identifier in registrations.items():
            assert stored[f"tenant_{field}"] == stored[field] == migrated_model_id(identifier)
        assert db.execute(text("SELECT ocr_id, tenant_ocr_id FROM usr_ai.t_ai_tenants WHERE id = 'other-owner'")).one() == ("ocr@OpenAI", None)


def test_cutover_locks_writers_until_defaults_and_models_commit(legacy_database: Engine) -> None:
    acquired = Event()
    release = Event()

    def pause_after_lock(_connection: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: bool) -> None:
        if statement.startswith("LOCK TABLE "):
            acquired.set()
            assert release.wait(timeout=8), "Timed out while waiting for competing write probe"

    event.listen(legacy_database, "after_cursor_execute", pause_after_lock)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            migration = pool.submit(upgrade_model_identity, legacy_database)
            try:
                assert acquired.wait(timeout=8), "Migration did not lock model reference tables"
                with pytest.raises(OperationalError, match="lock timeout"):
                    with legacy_database.begin() as competitor:
                        competitor.exec_driver_sql("SET LOCAL lock_timeout = '250ms'")
                        competitor.execute(text("UPDATE usr_ai.t_ai_tenants SET tenant_llm_id = 21 WHERE id = 'owner'"))
            finally:
                release.set()
            migration.result(timeout=8)
    finally:
        event.remove(legacy_database, "after_cursor_execute", pause_after_lock)

    with legacy_database.connect() as db:
        assert db.scalar(text("SELECT tenant_llm_id FROM usr_ai.t_ai_tenants WHERE id = 'owner'")) == migrated_model_id(17)
        assert db.scalar(text("SELECT count(*) FROM usr_ai.t_ai_tenant_models WHERE id = :id"), {"id": migrated_model_id(17)}) == 1


def test_historical_linking_revision_and_version_roll_back_together(legacy_database: Engine) -> None:
    previous, historical = "a7c9e1f3b5d7", "b8d0f2a4c6e8"
    with legacy_database.begin() as db:
        db.exec_driver_sql("DROP TABLE usr_ai.t_ai_tenant_models, usr_ai.t_ai_tenant_model_instances, usr_ai.t_ai_tenant_model_providers")
        db.execute(text("UPDATE usr_ai.alembic_version SET version_num = :version"), {"version": previous})
        # Two distinct credentials originally held the same public model name.
        db.execute(text("UPDATE usr_ai.t_ai_tenant_llms SET llm_name = 'chat-one' WHERE id = 17"))
        db.execute(text("UPDATE usr_ai.t_ai_tenant_llms SET llm_name = '' WHERE id = 21"))

    def historical_upgrade() -> None:
        with legacy_database.begin() as db:
            cfg = _alembic_config()
            cfg.attributes["connection"] = db
            command.upgrade(cfg, historical)

    with pytest.raises(RuntimeError, match="invalid name or capability"):
        historical_upgrade()
    with legacy_database.begin() as db:
        assert db.scalar(text("SELECT version_num FROM usr_ai.alembic_version")) == previous
        assert db.scalar(text("SELECT to_regclass('usr_ai.t_ai_tenant_models')")) is None
        db.execute(text("UPDATE usr_ai.t_ai_tenant_llms SET llm_name = 'repaired' WHERE id = 21"))
    historical_upgrade()
    historical_upgrade()
    with legacy_database.connect() as db:
        assert db.scalar(text("SELECT version_num FROM usr_ai.alembic_version")) == historical
        assert db.execute(text("SELECT id, model_name FROM usr_ai.t_ai_tenant_models ORDER BY id")).all() == [(17, "chat-one"), (21, "repaired"), (65, "old-embedding")]
