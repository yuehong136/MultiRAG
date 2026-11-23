"""Verify the one-time cutover and rollback against an owned PostgreSQL database."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from tests.support.database import _alembic_config
from tests.support.model_identity_migration import CUTOVER, PREVIOUS, config_digest, migrated_model_id, upgrade_model_identity
from tests.support.model_identity_migration import legacy_database as legacy_database

pytestmark = pytest.mark.integration


def test_cutover_preserves_models_connections_usage_and_rewrites_references(legacy_database: Engine) -> None:
    embedding_config = {
        "id": 65,
        "tenant_id": "owner",
        "llm_factory": "OpenAI",
        "llm_name": "old-embedding",
        "mdl_type": "embedding",
        "model_type": None,
        "api_key": "credential-one",
        "api_base": "https://office.invalid",
        "region": "",
        "max_tokens": 0,
        "instance_id": "first",
    }
    binding = {"tenant_model_id": 65, "model_reference": "old-embedding@OpenAI", "dimension": 3, "config_reference": config_digest(embedding_config)}
    with legacy_database.begin() as db:
        stmt = sa.text("INSERT INTO usr_ai.t_ai_memory_entry_revisions VALUES (:id, 'owner', :snapshot)").bindparams(sa.bindparam("snapshot", type_=JSONB))
        db.execute(
            stmt,
            [
                {"id": "same-config", "snapshot": {"content": "65 is plain text here", "model_binding": binding}},
                {"id": "changed-config", "snapshot": {"model_binding": {**binding, "config_reference": "old-config-is-stale"}}},
            ],
        )
    upgrade_model_identity(legacy_database)

    with legacy_database.connect() as db:
        observed = sa.inspect(db)
        assert not observed.has_table("t_ai_tenant_llms", schema="usr_ai")
        assert {index["referred_table"] for index in observed.get_foreign_keys("t_ai_tenant_models", schema="usr_ai")} == {
            "t_ai_tenant_model_providers",
            "t_ai_tenant_model_instances",
        }
        assert db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")) == CUTOVER
        models = {
            row.id: (row.model_name, row.instance_id, row.provider_id, row.model_type, row.status, row.extra, row.used_tokens) for row in db.execute(sa.text("SELECT * FROM usr_ai.t_ai_tenant_models"))
        }
        assert models == {
            migrated_model_id(17): ("same-model", "first", "provider", 9, "active", {"max_tokens": 120, "is_tools": True}, 74),
            migrated_model_id(21): ("same-model", "second", "provider", 1, "inactive", {"max_tokens": 240, "is_tools": False}, 91),
            migrated_model_id(65): ("old-embedding", "first", "provider", 2, "active", {"max_tokens": 0, "is_tools": False}, 33),
        }
        assert db.execute(sa.text("SELECT api_key, base_url FROM usr_ai.t_ai_tenant_model_instances WHERE id = 'first'")).one() == ("credential-one", "https://office.invalid")
        assert db.execute(sa.text("SELECT llm_id, tenant_llm_id, embd_id, tenant_embd_id FROM usr_ai.t_ai_tenants")).one() == (
            migrated_model_id(17),
            migrated_model_id(17),
            migrated_model_id(65),
            migrated_model_id(65),
        )
        assert db.scalar(sa.text("SELECT embedding_model_id FROM usr_ai.t_ai_skill_search_configs")) == migrated_model_id(65)
        assert db.scalar(sa.text("SELECT search_config FROM usr_ai.t_ai_search")) == {"chat_id": migrated_model_id(17), "comment": "model:opaque-office@OpenAI"}
        assert db.scalar(sa.text("SELECT dsl FROM usr_ai.t_ai_user_canvases"))["components"]["A"]["params"] == {"llm_id": migrated_model_id(21), "prompt": "model:opaque-office"}
        assert db.scalar(sa.text("SELECT dsl FROM usr_ai.t_ai_canvas_templates"))["components"]["A"]["params"] == {"llm_id": "", "prompt": "model:opaque-office"}
        assert db.scalar(sa.text("SELECT dsl FROM usr_ai.t_pipeline_operation_log")) == {
            "root": {"llm_id": migrated_model_id(17), "prompt": "17"},
            "completed": {"llm_id": migrated_model_id(29), "tenant_llm_id": migrated_model_id(29)},
        }
        artifact = db.execute(sa.text("SELECT model_binding, package FROM usr_ai.t_ai_memory_compaction_artifacts")).one()
        assert artifact.model_binding == {"tenant_model_id": migrated_model_id(17), "model_reference": "same-model@OpenAI", "config_reference": "historic-chat"}
        assert artifact.package == {"summary": "preserved compaction", "lineage": ["17"]}
        snapshot, snapshot_digest = db.execute(sa.text("SELECT snapshot_dsl, snapshot_digest FROM usr_ai.t_ai_agent_execution_origins")).one()
        assert snapshot["components"]["C"]["params"]["llm_id"] == migrated_model_id(17)
        assert snapshot_digest == config_digest(snapshot)
        states = dict(db.execute(sa.text("SELECT id, message_snapshot FROM usr_ai.t_ai_memory_entry_revisions")).tuples().all())
        assert states["same-config"] == {
            "content": "65 is plain text here",
            "model_binding": {**binding, "tenant_model_id": migrated_model_id(65), "config_reference": config_digest({**embedding_config, "id": migrated_model_id(65)})},
        }
        assert states["changed-config"]["model_binding"] == {**binding, "tenant_model_id": migrated_model_id(65), "config_reference": "old-config-is-stale"}
        cfg = _alembic_config()
        cfg.attributes["connection"] = db
        with pytest.raises(RuntimeError, match="snapshot"):
            command.downgrade(cfg, PREVIOUS)


def test_gateway_keeps_scope_alias_usage_and_registration_order(legacy_database: Engine) -> None:
    with legacy_database.begin() as db:
        db.exec_driver_sql("""
            INSERT INTO usr_ai.t_ai_gateway_model_settings (id, tenant_id, model_id, alias, preferred, rpm_limit, created_by)
                VALUES ('setting', 'owner', 17, 'office-chat', true, 600, 'operator');
            INSERT INTO usr_ai.t_ai_gateway_keys (id, tenant_id, name, secret_hash, allowed_model_ids, allowed_instance_ids, created_by)
                VALUES ('narrow', 'owner', 'narrow', repeat('a',64), '[17,99]', '["first"]', 'operator'),
                       ('empty', 'owner', 'empty', repeat('b',64), '[]', '[]', 'operator');
            INSERT INTO usr_ai.t_ai_gateway_request_logs (id, tenant_id, key_id, key_name, upstream_model_id, endpoint, status_code, started_at, duration_ms, total_tokens)
                VALUES ('active', 'owner', 'narrow', 'narrow', 17, 'chat', 200, now(), 3, 41),
                       ('stale', 'owner', 'narrow', 'narrow', 99, 'chat', 200, now(), 3, 12),
                       ('unresolved', 'owner', 'empty', 'empty', NULL, 'chat', 404, now(), 3, NULL);
            """)

    upgrade_model_identity(legacy_database)

    with legacy_database.begin() as reader:
        assert reader.exec_driver_sql("SELECT model_id, alias, preferred, rpm_limit FROM usr_ai.t_ai_gateway_model_settings").one() == (migrated_model_id(17), "office-chat", True, 600)
        assert reader.exec_driver_sql("SELECT id, allowed_model_ids, allowed_instance_ids, allow_all_models FROM usr_ai.t_ai_gateway_keys ORDER BY id").all() == [
            ("empty", [], [], False),
            ("narrow", [migrated_model_id(17), migrated_model_id(99)], ["first"], False),
        ]
        assert reader.exec_driver_sql("SELECT id, upstream_model_id, total_tokens FROM usr_ai.t_ai_gateway_request_logs ORDER BY id").all() == [
            ("active", migrated_model_id(17), 41),
            ("stale", migrated_model_id(99), 12),
            ("unresolved", None, None),
        ]
        assert reader.exec_driver_sql("SELECT id FROM usr_ai.t_ai_tenant_models ORDER BY registration_order").scalars().all() == [migrated_model_id(i) for i in (17, 21, 65)]
        assert (
            reader.exec_driver_sql("""
            INSERT INTO usr_ai.t_ai_tenant_models
                (id, provider_id, instance_id, model_name, model_type, status, extra)
                VALUES (repeat('a',32), 'provider', 'second', 'new-model', 1, 'active', '{}')
                RETURNING registration_order
            """).scalar_one()
            == 4
        )


def test_ambiguous_reference_aborts_without_remapping_or_dropping_legacy_data(legacy_database: Engine) -> None:
    with legacy_database.begin() as db:
        db.execute(sa.text('UPDATE usr_ai.t_ai_user_canvases SET dsl = \'{"llm_id":"same-model@OpenAI"}\''))
    with pytest.raises(RuntimeError, match="Ambiguous persisted model reference"):
        upgrade_model_identity(legacy_database)
    with legacy_database.connect() as db:
        assert db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")) == PREVIOUS
        assert db.scalars(sa.text("SELECT id FROM usr_ai.t_ai_tenant_llms ORDER BY id")).all() == [17, 21, 65]
        assert db.scalars(sa.text("SELECT id FROM usr_ai.t_ai_tenant_models ORDER BY id")).all() == [17, 21]
        assert db.scalar(sa.text("SELECT tenant_llm_id FROM usr_ai.t_ai_tenants")) == 17
        assert db.scalar(sa.text("SELECT dsl FROM usr_ai.t_ai_user_canvases")) == {"llm_id": "same-model@OpenAI"}


def test_inflight_compaction_blocks_cutover_and_preserves_legacy_schema(legacy_database: Engine) -> None:
    with legacy_database.begin() as db:
        db.execute(sa.text("UPDATE usr_ai.t_ai_memory_compaction_artifacts SET state = 'persistence_unknown'"))
    with pytest.raises(RuntimeError, match="Drain unfinished operations in t_ai_memory_compaction_artifacts"):
        upgrade_model_identity(legacy_database)
    with legacy_database.connect() as db:
        assert db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")) == PREVIOUS
        assert db.scalar(sa.text("SELECT count(*) FROM usr_ai.t_ai_tenant_llms")) == 3
        assert db.scalar(sa.text("SELECT model_binding->>'tenant_model_id' FROM usr_ai.t_ai_memory_compaction_artifacts")) == "17"


@pytest.mark.parametrize("state", ["queued", "claimed"])
def test_unfinished_maintenance_execution_blocks_cutover(legacy_database: Engine, state: str) -> None:
    with legacy_database.begin() as db:
        db.exec_driver_sql("CREATE TABLE usr_ai.t_ai_memory_maintenance_executions (job_id varchar(32) PRIMARY KEY, state varchar(32), receipt jsonb)")
        db.execute(sa.text("INSERT INTO usr_ai.t_ai_memory_maintenance_executions VALUES ('queued-analysis', :state, '{}')"), {"state": state})
    with pytest.raises(RuntimeError, match="Drain unfinished operations in t_ai_memory_maintenance_executions"):
        upgrade_model_identity(legacy_database)
    with legacy_database.connect() as db:
        assert db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")) == PREVIOUS
        assert db.scalar(sa.text("SELECT state FROM usr_ai.t_ai_memory_maintenance_executions")) == state
        assert db.scalar(sa.text("SELECT tenant_llm_id FROM usr_ai.t_ai_tenants")) == 17


def test_unlinked_opaque_name_is_never_migrated_as_provider_model_name(legacy_database: Engine) -> None:
    with legacy_database.begin() as db:
        db.execute(sa.text("UPDATE usr_ai.t_ai_tenant_llms SET llm_name = :opaque WHERE id = 65"), {"opaque": f"model:{'a' * 32}"})
    with pytest.raises(RuntimeError, match="unlinked model placeholders"):
        upgrade_model_identity(legacy_database)
    with legacy_database.connect() as db:
        assert db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")) == PREVIOUS
        assert db.scalars(sa.text("SELECT id FROM usr_ai.t_ai_tenant_models ORDER BY id")).all() == [17, 21]


def test_vision_only_default_cannot_be_migrated_as_chat(legacy_database: Engine) -> None:
    with legacy_database.begin() as db:
        db.execute(sa.text("""UPDATE usr_ai.t_ai_tenant_models SET model_types = '["image2text"]' WHERE id = 17"""))
    with pytest.raises(RuntimeError, match="configured capability"):
        upgrade_model_identity(legacy_database)
    with legacy_database.connect() as db:
        assert db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")) == PREVIOUS
        assert db.scalar(sa.text("SELECT tenant_llm_id FROM usr_ai.t_ai_tenants")) == 17


def test_dialog_vision_and_document_ocr_preserve_registered_models(legacy_database: Engine) -> None:
    with legacy_database.begin() as db:
        db.exec_driver_sql("""
            CREATE TABLE usr_ai.t_ai_dialogs (
                id varchar(32) PRIMARY KEY, tenant_id varchar(32), llm_id varchar(128), tenant_llm_id bigint, rerank_id varchar(128), tenant_rerank_id bigint
            );
            CREATE TABLE usr_ai.t_ai_knowledgebases (
                id varchar(32) PRIMARY KEY, tenant_id varchar(32), embd_id varchar(128), tenant_embd_id bigint, parser_config jsonb
            );
            CREATE TABLE usr_ai.t_ai_documents (
                id varchar(32) PRIMARY KEY, kb_id varchar(32), parser_config jsonb
            );
            UPDATE usr_ai.t_ai_tenant_models SET model_types = '["image2text"]' WHERE id = 21;
            INSERT INTO usr_ai.t_ai_dialogs VALUES ('vision-dialog', 'owner', 'same-model@cloud@OpenAI', NULL, '', NULL);
            INSERT INTO usr_ai.t_ai_tenant_llms VALUES (77, 'owner', 'MinerU', 'mineru-fixture', 'ocr', 'fixture-ocr', '', 0, 42, '1');
            INSERT INTO usr_ai.t_ai_knowledgebases VALUES ('kb', 'owner', 'old-embedding@OpenAI', 65, '{"layout_recognize":"DeepDOC"}');
            INSERT INTO usr_ai.t_ai_documents VALUES ('doc', 'kb', '{"layout_recognize":"mineru-fixture@MinerU","mineru_llm_name":"mineru-fixture@MinerU","context":"mineru-fixture@MinerU"}');
            """)
    upgrade_model_identity(legacy_database)
    with legacy_database.connect() as db:
        assert db.execute(sa.text("SELECT llm_id, tenant_llm_id FROM usr_ai.t_ai_dialogs")).one() == (migrated_model_id(21), migrated_model_id(21))
        assert db.scalar(sa.text("SELECT parser_config FROM usr_ai.t_ai_knowledgebases")) == {"layout_recognize": "DeepDOC"}
        assert db.scalar(sa.text("SELECT parser_config FROM usr_ai.t_ai_documents")) == {
            "layout_recognize": migrated_model_id(77),
            "mineru_llm_name": migrated_model_id(77),
            "context": "mineru-fixture@MinerU",
        }
