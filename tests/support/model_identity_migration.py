"""Historical model before/after registry fixtures for owned migration tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from tests.support.database import _alembic_config, scratch_database

PREVIOUS = "172b4d6f8a0c"
CUTOVER = "f7a9c2e4b6d8"


def migrated_model_id(previous_id: int) -> str:
    return uuid5(NAMESPACE_URL, f"multirag:tenant-model-id:{previous_id}").hex


def config_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def legacy_database(tmp_path: Path, postgres_service: None) -> Iterator[Engine]:
    with scratch_database(tmp_path, bootstrap=False) as engine:
        with engine.begin() as db:
            db.exec_driver_sql("""
                CREATE SCHEMA usr_ai;
                CREATE TABLE usr_ai.t_ai_llms (
                    id bigint PRIMARY KEY, fid varchar(128), llm_name varchar(128), tags text, is_tools boolean
                );
                CREATE TABLE usr_ai.t_ai_tenants (
                    id varchar(32) PRIMARY KEY,
                    llm_id varchar(128), tenant_llm_id bigint, embd_id varchar(128), tenant_embd_id bigint,
                    asr_id varchar(128), tenant_asr_id bigint, img2txt_id varchar(128), tenant_img2txt_id bigint,
                    rerank_id varchar(128), tenant_rerank_id bigint, tts_id varchar(128), tenant_tts_id bigint,
                    ocr_id varchar(128), tenant_ocr_id bigint
                );
                CREATE TABLE usr_ai.t_ai_tenant_llms (
                    id bigint PRIMARY KEY, tenant_id varchar(32), llm_factory varchar(128), llm_name varchar(128),
                    mdl_type varchar(32), api_key text, api_base varchar(2048),
                    max_tokens integer, used_tokens integer, status varchar(1)
                );
                CREATE TABLE usr_ai.t_ai_tenant_model_providers (
                    id varchar(32) PRIMARY KEY, tenant_id varchar(32), provider_name varchar(128), status varchar(8)
                );
                CREATE TABLE usr_ai.t_ai_tenant_model_instances (
                    id varchar(32) PRIMARY KEY, provider_id varchar(32) REFERENCES usr_ai.t_ai_tenant_model_providers(id),
                    instance_name varchar(128), api_key text, base_url varchar(2048), region varchar(128), status varchar(8)
                );
                CREATE TABLE usr_ai.t_ai_tenant_models (
                    id bigint PRIMARY KEY REFERENCES usr_ai.t_ai_tenant_llms(id),
                    instance_id varchar(32) REFERENCES usr_ai.t_ai_tenant_model_instances(id),
                    model_name varchar(128), model_types json, is_tools boolean, UNIQUE(instance_id, model_name)
                );
                CREATE TABLE usr_ai.t_ai_search (id varchar(32) PRIMARY KEY, tenant_id varchar(32), search_config jsonb);
                CREATE TABLE usr_ai.t_ai_user_canvases (id varchar(32) PRIMARY KEY, user_id varchar(32), dsl jsonb);
                CREATE TABLE usr_ai.t_ai_canvas_templates (id varchar(32) PRIMARY KEY, dsl jsonb);
                CREATE TABLE usr_ai.t_pipeline_operation_log (id varchar(32) PRIMARY KEY, tenant_id varchar(32), dsl jsonb);
                CREATE TABLE usr_ai.t_ai_agent_execution_origins (
                    id varchar(32) PRIMARY KEY, tenant_id varchar(32), snapshot_digest varchar(64), snapshot_dsl jsonb
                );
                CREATE TABLE usr_ai.t_ai_memory_entry_revisions (id varchar(32) PRIMARY KEY, tenant_id varchar(32), message_snapshot jsonb);
                CREATE TABLE usr_ai.t_ai_memory_compaction_artifacts (
                    artifact_id varchar(32) PRIMARY KEY, tenant_id varchar(32), state varchar(32), model_binding jsonb, package jsonb
                );
                CREATE TABLE usr_ai.t_ai_skill_search_configs (
                    id varchar(32) PRIMARY KEY, tenant_id varchar(32), embedding_model_id bigint, rerank_model_id bigint
                );
                INSERT INTO usr_ai.t_ai_tenant_model_providers VALUES ('provider', 'owner', 'OpenAI', 'active');
                INSERT INTO usr_ai.t_ai_tenant_model_instances VALUES
                    ('first', 'provider', 'office', 'credential-one', 'https://office.invalid', '', 'active'),
                    ('second', 'provider', 'cloud', 'credential-two', 'https://cloud.invalid', '', 'active');
                INSERT INTO usr_ai.t_ai_tenant_llms VALUES
                    (17, 'owner', 'OpenAI', 'model:opaque-office', 'chat', 'old-snapshot', '', 120, 74, '1'),
                    (21, 'owner', 'OpenAI', 'model:opaque-cloud', 'chat', 'old-snapshot', '', 240, 91, '0'),
                    (65, 'owner', 'OpenAI', 'old-embedding', 'embedding', 'credential-one', 'https://office.invalid', 0, 33, '1');
                INSERT INTO usr_ai.t_ai_tenant_models VALUES
                    (17, 'first', 'same-model', '["chat","image2text"]', true),
                    (21, 'second', 'same-model', '["chat"]', false);
                INSERT INTO usr_ai.t_ai_tenants (id, llm_id, tenant_llm_id, embd_id)
                    VALUES ('owner', 'same-model@OpenAI', 17, 'old-embedding@OpenAI');
                INSERT INTO usr_ai.t_ai_search VALUES ('search', 'owner', '{"chat_id":"model:opaque-office@OpenAI","comment":"model:opaque-office@OpenAI"}');
                INSERT INTO usr_ai.t_ai_user_canvases VALUES
                    ('canvas', 'owner', '{"components":{"A":{"params":{"llm_id":"same-model@cloud@OpenAI","prompt":"model:opaque-office"}}}}');
                INSERT INTO usr_ai.t_ai_canvas_templates VALUES
                    ('template', '{"components":{"A":{"params":{"llm_id":"same-model@cloud@OpenAI","tenant_llm_id":21,"prompt":"model:opaque-office"}}}}');
                INSERT INTO usr_ai.t_pipeline_operation_log VALUES
                    ('pipeline', 'owner', '{"root":{"llm_id":17,"prompt":"17"},"completed":{"llm_id":29,"tenant_llm_id":29}}');
                INSERT INTO usr_ai.t_ai_memory_compaction_artifacts VALUES
                    ('artifact', 'owner', 'generated', '{"tenant_model_id":"17","model_reference":"same-model@OpenAI","config_reference":"historic-chat"}',
                     '{"summary":"preserved compaction","lineage":["17"]}');
                INSERT INTO usr_ai.t_ai_skill_search_configs VALUES ('skills', 'owner', 65, NULL);
                """)
            dsl = {"components": {"C": {"params": {"llm_id": "same-model@office@OpenAI"}}}}
            db.execute(
                sa.text("INSERT INTO usr_ai.t_ai_agent_execution_origins VALUES ('execution', 'owner', :digest, :dsl)").bindparams(sa.bindparam("dsl", type_=JSONB)),
                {"digest": config_digest(dsl), "dsl": dsl},
            )
            cfg = _alembic_config()
            cfg.attributes["connection"] = db
            scripts = ScriptDirectory.from_config(cfg)
            with Operations.context(MigrationContext.configure(db)):
                for revision in ("d1f3a5c7e9b2", "f3b5d7e9a1c2"):
                    scripts.get_revision(revision).module.upgrade()
            command.stamp(cfg, PREVIOUS)
        yield engine


def upgrade_model_identity(engine: Engine) -> None:
    with engine.begin() as db:
        cfg = _alembic_config()
        cfg.attributes["connection"] = db
        command.upgrade(cfg, CUTOVER)
