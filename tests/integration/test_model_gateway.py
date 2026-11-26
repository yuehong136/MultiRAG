"""Owned PostgreSQL evidence for the model gateway.

The data plane and the management routes run against a scratch database; the
upstream model service is a controlled transport. The caller side is the real
OpenAI Python SDK, driven through the in-process gateway application.
"""

import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import anthropic
import httpx
import openai
import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from api.db.db_models import GatewayDailyUsage, GatewayKey, GatewayModelHealth, GatewayModelSetting, GatewayRequestLog, GatewaySetting
from api.db.services import model_provider_service
from api.db.services.model_deletion_policy import collect_blocking_model_references
from api.model_gateway.health import HealthChecker, HealthResult
from api.model_gateway.keys import hash_key
from api.model_gateway.router import Router
from api.model_gateway.store import SqlGatewayStore
from common.app_config import get_app_config
from tests.support.database import _alembic_config
from tests.support.model_gateway import MANAGEMENT_PREFIX as _PREFIX
from tests.support.model_gateway import anthropic_sdk, data_plane, instance_id, logs, management, register_model, sdk
from tests.support.provider_instances import owned_engine as owned_engine
from tests.support.provider_instances import tenant
from tests.support.resource_journal import ResourceJournal

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_background_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """The management app reads the process configuration: keep it from fetching prices or showing health."""
    settings = get_app_config().model_gateway
    monkeypatch.setattr(settings, "health_check_interval_seconds", 0.0)
    monkeypatch.setattr(settings, "price_map_refresh_hours", 0.0)


_GATEWAY_MODELS = (GatewayModelSetting, GatewaySetting, GatewayKey, GatewayRequestLog, GatewayDailyUsage, GatewayModelHealth)


_DELTA = (
    b'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"qwen-upstream","choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},"finish_reason":null}],"usage":null}\n\n'
)
_STOP = b'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"qwen-upstream","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":null}\n\n'
_USAGE_ONLY = b'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"qwen-upstream","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
_DONE = b"data: [DONE]\n\n"
# float32 little-endian [1.0, 2.0], the encoding the OpenAI SDK asks for by default.
_EMBEDDING_B64 = "AACAPwAAAEA="


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class _Upstream:
    """A controlled OpenAI-compatible model service."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        # Hosts that answer 503 for now.
        self.failing: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "authorization": request.headers.get("authorization"), "body": body})
        if request.url.host in self.failing:
            return httpx.Response(503, json={"error": {"message": "overloaded", "code": "overloaded"}})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200, json={"object": "list", "model": body["model"], "data": [{"object": "embedding", "index": 0, "embedding": _EMBEDDING_B64}], "usage": {"prompt_tokens": 4, "total_tokens": 4}}
            )
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Chunks([_DELTA, _STOP, _USAGE_ONLY, _DONE]))
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )


def _revisions() -> tuple[Any, Any]:
    scripts = ScriptDirectory.from_config(_alembic_config())
    tables, catalog = scripts.get_revision("d1f3a5c7e9b2"), scripts.get_revision("f3b5d7e9a1c2")
    assert tables is not None and tables.down_revision == "c9e1f3a5b7d9"
    assert catalog is not None and catalog.down_revision == "d1f3a5c7e9b2"
    return tables.module, catalog.module


def _point_at(monkeypatch: pytest.MonkeyPatch, connection: sa.Connection, schema: str, *migrations: Any) -> None:
    for migration in migrations:
        monkeypatch.setattr(migration, "SCHEMA", schema)
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))


def _assert_schema_matches_models(connection: sa.Connection, schema: str) -> None:
    inspector = sa.inspect(connection)
    for model in _GATEWAY_MODELS:
        table = model.__table__
        actual = {column["name"]: column for column in inspector.get_columns(table.name, schema=schema)}
        assert set(actual) == set(table.columns.keys()), table.name
        for column in table.columns:
            assert actual[column.name]["type"].compile(dialect=connection.dialect) == column.type.compile(dialect=connection.dialect), column.name
            assert actual[column.name]["nullable"] == column.nullable, column.name
        expected_indexes = {(tuple(column.name for column in index.columns), bool(index.unique)) for index in table.indexes}
        actual_indexes = {(tuple(index["column_names"]), bool(index["unique"])) for index in inspector.get_indexes(table.name, schema=schema) if not index.get("duplicates_constraint")}
        assert actual_indexes == expected_indexes, table.name
        expected_unique = {tuple(column.name for column in item.columns) for item in table.constraints if isinstance(item, sa.UniqueConstraint)}
        assert {tuple(item["column_names"]) for item in inspector.get_unique_constraints(table.name, schema=schema)} == expected_unique, table.name
        assert {item["name"] for item in inspector.get_check_constraints(table.name, schema=schema)} == {item.name for item in table.constraints if isinstance(item, sa.CheckConstraint)}
    assert not inspector.has_table("t_ai_gateway_models", schema=schema)


def test_gateway_schema_matches_models_on_both_bootstrap_paths(owned_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh metadata and sequential gateway upgrades agree on constraints and types."""
    tables_revision, catalog_revision = _revisions()
    identity_revision = ScriptDirectory.from_config(_alembic_config()).get_revision("f7a9c2e4b6d8").module
    cost = ScriptDirectory.from_config(_alembic_config()).get_revision("0a2c4e6f8b1d")
    assert cost is not None and cost.down_revision == "f7a9c2e4b6d8"
    cost_revision = cost.module
    deployments = ScriptDirectory.from_config(_alembic_config()).get_revision("1b3d5f7a9c2e")
    assert deployments is not None and deployments.down_revision == "0a2c4e6f8b1d"
    deployments_revision = deployments.module
    health = ScriptDirectory.from_config(_alembic_config()).get_revision("2c4e6a8b0d1f")
    assert health is not None and health.down_revision == "1b3d5f7a9c2e"
    health_revision = health.module
    classes = ScriptDirectory.from_config(_alembic_config()).get_revision("3d5f7b9c1e2a")
    assert classes is not None and classes.down_revision == "2c4e6a8b0d1f"
    classes_revision = classes.module
    journal = ResourceJournal("model_gateway_schema")
    for model_first in (False, True):
        schema = "model_gateway_" + uuid.uuid4().hex[:12]
        journal.event("reserve-schema", schema=schema, database=owned_engine.url.database, model_first=model_first)
        with owned_engine.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
            _point_at(monkeypatch, connection, schema, tables_revision, catalog_revision, identity_revision, cost_revision, deployments_revision, health_revision, classes_revision)
            if model_first:
                metadata = sa.MetaData()
                for model in _GATEWAY_MODELS:
                    model.__table__.to_metadata(metadata, schema=schema)
                metadata.create_all(connection)
            else:
                tables_revision.upgrade()
                catalog_revision.upgrade()
                catalog_revision.upgrade()
                identity_revision._migrate_gateway_references(connection, identity_revision._gateway_reference_tables(connection))
                # Calls made before costs existed are folded into the daily sums.
                logs_table = sa.Table("t_ai_gateway_request_logs", sa.MetaData(), autoload_with=connection, schema=schema)
                old = datetime(2026, 11, 20, 23, 30, tzinfo=UTC)
                call = {"tenant_id": "t", "key_id": "key-0", "key_name": "runner", "endpoint": "chat.completions", "started_at": old, "duration_ms": 10}
                for row in (
                    {
                        **call,
                        "id": "r1",
                        "upstream_model_id": "a" * 32,
                        "upstream_model_name": "qwen",
                        "status_code": 200,
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                        "usage_source": "reported",
                    },
                    {**call, "id": "r2", "upstream_model_id": "a" * 32, "upstream_model_name": "qwen", "status_code": 502, "error_type": "upstream_error", "usage_source": "unavailable"},
                    {**call, "id": "r3", "status_code": 404, "error_type": "invalid_request_error", "usage_source": "unavailable", "started_at": old + timedelta(hours=1)},
                ):
                    connection.execute(logs_table.insert().values(**row))
            # Running it again, or over tables created from the models, changes nothing.
            cost_revision.upgrade()
            cost_revision.upgrade()
            if not model_first:
                # A preferred model used to take every call to its shared name; order 1 keeps it first.
                before = sa.Table("t_ai_gateway_model_settings", sa.MetaData(), autoload_with=connection, schema=schema)
                connection.execute(before.insert().values(id="p-1", tenant_id="t", model_id="e" * 32, preferred=True, created_by="u"))
                connection.execute(before.insert().values(id="p-2", tenant_id="t", model_id="f" * 32, created_by="u"))
            deployments_revision.upgrade()
            deployments_revision.upgrade()
            health_revision.upgrade()
            health_revision.upgrade()
            classes_revision.upgrade()
            classes_revision.upgrade()
            _assert_schema_matches_models(connection, schema)
            health_table = sa.Table("t_ai_gateway_model_health", sa.MetaData(), autoload_with=connection, schema=schema)
            limited = {"id": "h-1", "tenant_id": "t", "model_id": "a" * 32, "status": "rate_limited", "failure_class": "rate_limit", "response_time_ms": 3, "checked_at": datetime.now(UTC)}
            connection.execute(health_table.insert().values(**limited))
            assert connection.scalar(sa.select(health_table.c.source)) == "probe"
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(health_table.insert().values(**{**limited, "id": "h-2", "model_id": "b" * 32, "status": "broken"}))
            if not model_first:
                after = sa.Table("t_ai_gateway_model_settings", sa.MetaData(), autoload_with=connection, schema=schema)
                assert dict(connection.execute(sa.select(after.c.id, after.c.deployment_order)).all()) == {"p-1": 1, "p-2": None}
                connection.execute(after.delete())
            if not model_first:
                daily = sa.Table("t_ai_gateway_daily_usage", sa.MetaData(), autoload_with=connection, schema=schema)
                folded = {
                    (str(row.date), row.model_id): (
                        row.api_requests,
                        row.successful_requests,
                        row.failed_requests,
                        row.usage_unknown_requests,
                        row.total_tokens,
                        row.total_response_time_ms,
                        row.model_name,
                    )
                    for row in connection.execute(sa.select(daily)).all()
                }
                assert folded == {("2026-11-20", "a" * 32): (2, 1, 1, 1, 5, 20, "qwen"), ("2026-11-21", ""): (1, 0, 1, 1, 0, 10, None)}
                connection.execute(daily.delete())
                connection.execute(logs_table.delete())

            settings_table = sa.Table("t_ai_gateway_model_settings", sa.MetaData(), autoload_with=connection, schema=schema)
            tenant_table = sa.Table("t_ai_gateway_settings", sa.MetaData(), autoload_with=connection, schema=schema)
            keys = sa.Table("t_ai_gateway_keys", sa.MetaData(), autoload_with=connection, schema=schema)
            connection.execute(keys.insert().values(id="key-1", tenant_id="t", name="runner", secret_hash="a" * 64, created_by="u"))
            assert connection.scalar(sa.select(keys.c.allowed_instance_ids)) == []
            connection.execute(settings_table.insert().values(id="s-1", tenant_id="t", model_id="a" * 32, created_by="u"))
            connection.execute(settings_table.insert().values(id="s-2", tenant_id="t", model_id="b" * 32, created_by="u"))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(settings_table.insert().values(id="s-3", tenant_id="t", model_id="a" * 32, created_by="u"))
            # Models given the same alias are the deployments of that name.
            connection.execute(settings_table.update().where(settings_table.c.id == "s-1").values(alias="chat-default", create_time=1))
            connection.execute(settings_table.update().where(settings_table.c.id == "s-2").values(alias="chat-default", create_time=2))
            connection.execute(settings_table.insert().values(id="s-4", tenant_id="other", model_id="a" * 32, alias="chat-default", created_by="u"))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(settings_table.update().where(settings_table.c.id == "s-2").values(rpm_limit=0))
            assert connection.scalar(sa.select(settings_table.c.enabled).where(settings_table.c.id == "s-2")) is True
            connection.execute(tenant_table.insert().values(id="g-1", tenant_id="t"))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(tenant_table.insert().values(id="g-2", tenant_id="t"))
            assert connection.scalar(sa.select(tenant_table.c.new_models_enabled)) is True

            with pytest.raises(RuntimeError, match="persisted model gateway data"):
                catalog_revision.downgrade()
            # The older schema cannot say "rate limited": such results go, and the status rule is the old one again.
            classes_revision.downgrade()
            assert connection.scalar(sa.select(sa.func.count()).select_from(health_table)) == 0
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    sa.text(f"INSERT INTO {schema}.t_ai_gateway_model_health (id, tenant_id, model_id, status, response_time_ms, checked_at) VALUES ('h-3', 't', 'c', 'rate_limited', 1, now())")
                )
            health_revision.downgrade()
            assert not sa.inspect(connection).has_table("t_ai_gateway_model_health", schema=schema)
            assert "previous_secret_hash" not in {column["name"] for column in sa.inspect(connection).get_columns("t_ai_gateway_keys", schema=schema)}
            # Going back, an alias is unique again: the earliest model keeps it.
            deployments_revision.downgrade()
            assert dict(connection.execute(sa.select(settings_table.c.id, settings_table.c.alias)).all()) == {"s-1": "chat-default", "s-2": None, "s-4": "chat-default"}
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(settings_table.update().where(settings_table.c.id == "s-2").values(alias="chat-default"))
            assert "attempts" not in {column["name"] for column in sa.inspect(connection).get_columns("t_ai_gateway_request_logs", schema=schema)}
            for table in (settings_table, tenant_table, keys):
                connection.execute(table.delete())
            cost_revision.downgrade()
            assert not sa.inspect(connection).has_table("t_ai_gateway_daily_usage", schema=schema)
            catalog_revision.downgrade()
            assert sa.inspect(connection).has_table("t_ai_gateway_models", schema=schema)
            assert "allowed_instance_ids" not in {column["name"] for column in sa.inspect(connection).get_columns("t_ai_gateway_keys", schema=schema)}
            tables_revision.downgrade()
            assert sa.inspect(connection).get_table_names(schema=schema) == []
            connection.execute(sa.schema.DropSchema(schema))
        journal.event("schema-cleanup-readback", schema=schema, absent=True)


def test_published_models_and_key_scopes_are_carried_into_the_catalog(owned_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    tables_revision, catalog_revision = _revisions()
    journal = ResourceJournal("model_gateway_carry_over")
    schema = "model_gateway_" + uuid.uuid4().hex[:12]
    journal.event("reserve-schema", schema=schema, database=owned_engine.url.database)
    with owned_engine.begin() as connection:
        connection.execute(sa.schema.CreateSchema(schema))
        # The registrations the carry-over reads, reduced to the columns it uses.
        metadata = sa.MetaData(schema=schema)
        llms = sa.Table("t_ai_tenant_llms", metadata, sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("tenant_id", sa.String(32)))
        models = sa.Table("t_ai_tenant_models", metadata, sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("model_name", sa.String(128)))
        metadata.create_all(connection)
        connection.execute(llms.insert(), [{"id": 1, "tenant_id": "gw"}, {"id": 2, "tenant_id": "gw"}, {"id": 3, "tenant_id": "gw"}, {"id": 10, "tenant_id": "untouched"}])
        connection.execute(models.insert(), [{"id": 1, "model_name": "qwen-plus"}, {"id": 2, "model_name": "bge-m3"}, {"id": 3, "model_name": "never-published"}, {"id": 10, "model_name": "x"}])
        _point_at(monkeypatch, connection, schema, tables_revision, catalog_revision)
        tables_revision.upgrade()
        published, keys, _logs = tables_revision._tables()
        connection.execute(
            published.insert(),
            [
                {
                    "id": "p-chat",
                    "tenant_id": "gw",
                    "name": "chat-default",
                    "model_id": 1,
                    "model_type": "chat",
                    "status": "active",
                    "rpm_limit": 600,
                    "description": "main",
                    "created_by": "op",
                    "create_time": 1,
                },
                {
                    "id": "p-old",
                    "tenant_id": "gw",
                    "name": "chat-legacy",
                    "model_id": 1,
                    "model_type": "chat",
                    "status": "inactive",
                    "rpm_limit": None,
                    "description": None,
                    "created_by": "op",
                    "create_time": 0,
                },
                {
                    "id": "p-embed",
                    "tenant_id": "gw",
                    "name": "bge-m3",
                    "model_id": 2,
                    "model_type": "embedding",
                    "status": "inactive",
                    "rpm_limit": None,
                    "description": None,
                    "created_by": "op",
                    "create_time": 2,
                },
            ],
        )
        connection.execute(
            keys.insert(),
            [
                {
                    "id": "k-narrow",
                    "tenant_id": "gw",
                    "name": "narrow",
                    "secret_hash": "a" * 64,
                    "created_by": "op",
                    "allow_all_models": False,
                    "allowed_model_ids": ["p-chat", "p-embed", "p-old", "gone"],
                },
                {"id": "k-all", "tenant_id": "gw", "name": "all", "secret_hash": "b" * 64, "created_by": "op", "allow_all_models": True, "allowed_model_ids": []},
            ],
        )

        catalog_revision.upgrade()

        settings_table, _tenant_table = catalog_revision._tables()
        carried = {row.model_id: row for row in connection.execute(sa.select(settings_table)).all()}
        assert set(carried) == {1, 2, 3}
        # The active publication wins; its name becomes the alias, its limits stay.
        assert (carried[1].tenant_id, carried[1].enabled, carried[1].alias, carried[1].rpm_limit, carried[1].description, carried[1].created_by) == ("gw", True, "chat-default", 600, "main", "op")
        # A publication named like the model needs no alias; an inactive one is a closed model.
        assert (carried[2].enabled, carried[2].alias) == (False, None)
        # Nothing could call an unpublished model before, so it starts closed.
        assert (carried[3].enabled, carried[3].alias) == (False, None)
        scopes = {
            row.id: (row.allow_all_models, row.allowed_model_ids, row.allowed_instance_ids)
            for row in connection.execute(sa.select(sa.Table("t_ai_gateway_keys", sa.MetaData(), autoload_with=connection, schema=schema))).all()
        }
        assert scopes == {"k-narrow": (False, [1, 2], []), "k-all": (True, [], [])}
        assert not sa.inspect(connection).has_table("t_ai_gateway_models", schema=schema)
        connection.execute(sa.schema.DropSchema(schema, cascade=True))
    journal.event("schema-cleanup-readback", schema=schema, absent=True)


def _run(coroutine: Any) -> Any:
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
        return runner.run(coroutine)


def test_gateway_serves_registered_models_with_scoped_keys(owned_engine: Engine) -> None:
    owner, other_owner = uuid.uuid4().hex, uuid.uuid4().hex
    with Session(owned_engine) as db:
        db.add_all([tenant(owner), tenant(other_owner)])
        chat_id = register_model(db, owner, "VLLM", "qwen-upstream___VLLM", "chat", "http://models.internal:8000", "upstream-secret")
        closed_id = register_model(db, owner, "VLLM", "qwen-closed", "chat", "http://models.internal:8000", "upstream-secret")
        embed_id = register_model(db, owner, "Xinference", "bge-upstream", "embedding", "http://models.internal:9997/v1", "x")
        hosted_id = register_model(db, owner, "Anthropic", "claude-hosted", "chat", "https://api.anthropic.com", "hosted-secret")
        twin_id = register_model(db, owner, "OpenAI-API-Compatible", "qwen-upstream", "chat", "http://twin.internal:8000", "twin-secret")
        foreign_id = register_model(db, other_owner, "VLLM", "foreign-model", "chat", "http://models.internal:8001", "foreign-secret")
        vllm = instance_id(db, owner, "VLLM")
        db.commit()
    upstream = _Upstream()
    hello = [{"role": "user", "content": "hello"}]

    with management(owned_engine, owner) as admin:
        # Every registered model is in the catalog; nothing has to be published.
        catalog = admin.get(f"{_PREFIX}/catalog").json()["data"]
        models = {item["model_id"]: item for item in catalog["models"]}
        assert set(models) == {chat_id, closed_id, embed_id, hosted_id, twin_id}
        assert catalog["new_models_enabled"] is True
        assert {item["provider"] for item in catalog["connections"]} == {"VLLM", "Xinference", "Anthropic", "OpenAI-API-Compatible"}
        chat = models[chat_id]
        assert (chat["call_name"], chat["state"], chat["enabled_by_default"], chat["shares_name"], chat["servable_types"]) == ("qwen-upstream", "callable", True, True, ["chat"])
        assert chat["names"] == ["qwen-upstream", "VLLM/qwen-upstream", "VLLM/default/qwen-upstream"]
        assert models[twin_id]["call_name"] == "OpenAI-API-Compatible/qwen-upstream"
        # Two models share the bare name, so a call to it may be served by either.
        assert chat["pools"] == models[twin_id]["pools"] == [{"name": "qwen-upstream", "deployments": 2}]
        assert (chat["weight"], chat["order"], models[embed_id]["pools"]) == (1, None, [])
        # Order 1 makes this one the first choice and leaves the twin as its stand-in.
        ordered = admin.patch(f"{_PREFIX}/catalog/models/{chat_id}", json={"order": 1, "weight": 3}).json()["data"]
        assert (ordered["order"], ordered["weight"]) == (1, 3)
        assert admin.patch(f"{_PREFIX}/catalog/models/{chat_id}", json={"order": 0}).status_code in (400, 422)
        assert (models[hosted_id]["state"], models[hosted_id]["reason"]) == ("unavailable", "provider_not_supported")

        closed = admin.patch(f"{_PREFIX}/catalog/models/{closed_id}", json={"enabled": False}).json()["data"]
        assert (closed["state"], closed["enabled_by_default"]) == ("closed", False)
        aliased = admin.patch(f"{_PREFIX}/catalog/models/{embed_id}", json={"alias": "embed-default", "description": "bge"}).json()["data"]
        assert (aliased["call_name"], aliased["names"][1], aliased["description"], aliased["enabled"]) == ("embed-default", "bge-upstream", "bge", True)
        # A name another model answers to cannot become an alias; other owners' models do not exist here.
        assert admin.patch(f"{_PREFIX}/catalog/models/{twin_id}", json={"alias": "qwen-upstream"}).status_code == 409
        assert admin.patch(f"{_PREFIX}/catalog/models/{foreign_id}", json={"enabled": False}).status_code == 404
        assert admin.patch(f"{_PREFIX}/catalog/models/{twin_id}", json={"preferred": True}).json()["data"]["call_name"] == "qwen-upstream"
        assert admin.patch(f"{_PREFIX}/catalog/models/{twin_id}", json={"preferred": False}).json()["data"]["call_name"] == "OpenAI-API-Compatible/qwen-upstream"

        full_key = admin.post(f"{_PREFIX}/keys", json={"name": "eval-runner", "allow_all_models": True}).json()["data"]
        connection_key = admin.post(f"{_PREFIX}/keys", json={"name": "vllm-only", "allowed_instance_ids": [vllm]}).json()["data"]
        narrow_key = admin.post(f"{_PREFIX}/keys", json={"name": "indexing-job", "tags": {"purpose": "index"}, "allowed_model_ids": [embed_id]}).json()["data"]
        empty_key = admin.post(f"{_PREFIX}/keys", json={"name": "no-models"}).json()["data"]
        assert admin.post(f"{_PREFIX}/keys", json={"name": "bad-model", "allowed_model_ids": [foreign_id]}).status_code == 400
        assert admin.post(f"{_PREFIX}/keys", json={"name": "bad-connection", "allowed_instance_ids": ["missing"]}).status_code == 400
        assert admin.post(f"{_PREFIX}/keys", json={"name": "eval-runner"}).status_code == 409
        listed = {item["name"]: item for item in admin.get(f"{_PREFIX}/keys").json()["data"]}
        assert set(listed) == {"eval-runner", "vllm-only", "indexing-job", "no-models"}
        assert (listed["vllm-only"]["allowed_instance_ids"], listed["indexing-job"]["allowed_model_ids"]) == ([vllm], [embed_id])
        assert all("key" not in item and full_key["key"] not in json.dumps(item) for item in listed.values())
    with Session(owned_engine) as reader:
        stored = reader.get(GatewayKey, full_key["id"])
        assert stored is not None and stored.secret_hash == hash_key(full_key["key"])
        assert full_key["key"] not in json.dumps(stored.to_dict(), default=str)
        assert stored.description is None and stored.tags is None and stored.last_used_at is None

    with data_plane(owned_engine, httpx.MockTransport(upstream)) as gateway:
        client = sdk(gateway, full_key["key"])
        assert {model.id for model in client.models.list().data} == {"qwen-upstream", "embed-default", "OpenAI-API-Compatible/qwen-upstream"}
        assert [model.id for model in sdk(gateway, connection_key["key"]).models.list().data] == ["qwen-upstream"]
        assert [model.id for model in sdk(gateway, narrow_key["key"]).models.list().data] == ["embed-default"]
        assert sdk(gateway, empty_key["key"]).models.list().data == []

        completion = client.chat.completions.create(model="qwen-upstream", messages=hello, user="job-7")
        assert completion.choices[0].message.content == "hi"
        assert completion.usage is not None and completion.usage.total_tokens == 5
        assert upstream.requests[-1] == {
            "url": "http://models.internal:8000/v1/chat/completions",
            "authorization": "Bearer upstream-secret",
            "body": {"model": "qwen-upstream", "messages": hello, "user": "job-7"},
        }
        # A provider-qualified name reaches the model it names.
        client.chat.completions.create(model="OpenAI-API-Compatible/qwen-upstream", messages=hello)
        assert (upstream.requests[-1]["url"], upstream.requests[-1]["authorization"], upstream.requests[-1]["body"]["model"]) == (
            "http://twin.internal:8000/v1/chat/completions",
            "Bearer twin-secret",
            "qwen-upstream",
        )

        chunks = list(client.chat.completions.create(model="VLLM/default/qwen-upstream", messages=hello, stream=True))
        assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks) == "hi"
        # The gateway asked upstream for usage; the caller did not, and sees no usage chunk.
        assert upstream.requests[-1]["body"]["stream_options"] == {"include_usage": True}
        assert all(chunk.choices for chunk in chunks)

        # The SDK requests base64 embeddings by default and decodes them itself.
        embedding = client.embeddings.create(model="embed-default", input="text to embed")
        assert embedding.data[0].embedding == [1.0, 2.0]
        assert upstream.requests[-1]["url"] == "http://models.internal:9997/v1/embeddings"
        assert upstream.requests[-1]["authorization"] is None
        assert upstream.requests[-1]["body"] == {"model": "bge-upstream", "input": "text to embed", "encoding_format": "base64"}

        forwarded = len(upstream.requests)
        with pytest.raises(openai.PermissionDeniedError) as denied:
            sdk(gateway, narrow_key["key"]).chat.completions.create(model="qwen-upstream", messages=[])
        assert denied.value.code == "model_not_allowed"
        with pytest.raises(openai.PermissionDeniedError):
            sdk(gateway, connection_key["key"]).embeddings.create(model="embed-default", input="x")
        for name in ("qwen-closed", "gpt-4o"):
            with pytest.raises(openai.NotFoundError) as missing:
                client.chat.completions.create(model=name, messages=[])
            assert missing.value.code == "model_not_found"
        with pytest.raises(openai.InternalServerError) as unsupported:
            client.chat.completions.create(model="claude-hosted", messages=[])
        assert (unsupported.value.status_code, unsupported.value.code) == (503, "model_unavailable")
        with pytest.raises(openai.BadRequestError) as mismatch:
            client.embeddings.create(model="qwen-upstream", input="x")
        assert mismatch.value.code == "model_endpoint_mismatch"
        with pytest.raises(openai.AuthenticationError) as forged:
            sdk(gateway, full_key["key"][:-1] + ("A" if full_key["key"][-1] != "A" else "B")).models.list()
        assert forged.value.code == "invalid_api_key"
        assert len(upstream.requests) == forwarded

        # The first choice is down: the same call is answered by the other deployment of the name.
        upstream.failing.add("models.internal")
        rescued = client.chat.completions.create(model="qwen-upstream", messages=hello)
        assert rescued.choices[0].message.content == "hi"
        assert [item["url"] for item in upstream.requests[forwarded:]] == ["http://models.internal:8000/v1/chat/completions", "http://twin.internal:8000/v1/chat/completions"]
        # A key scoped to the failing connection has nowhere else to go.
        with pytest.raises(openai.InternalServerError) as stranded:
            sdk(gateway, connection_key["key"]).chat.completions.create(model="qwen-upstream", messages=hello)
        assert (stranded.value.status_code, stranded.value.code) == (502, "upstream_error")
        assert len(upstream.requests) == forwarded + 3
        upstream.failing.clear()

        # Anthropic's SDK calls the same model through /v1/messages; the upstream still sees a chat completion.
        message = anthropic_sdk(gateway, full_key["key"]).messages.create(model="qwen-upstream", max_tokens=32, system="be brief", messages=[{"role": "user", "content": "hello"}])
        assert (message.content[0].text, message.stop_reason, message.usage.input_tokens, message.usage.output_tokens) == ("hi", "end_turn", 3, 2)
        assert upstream.requests[-1]["url"] == "http://models.internal:8000/v1/chat/completions"
        assert upstream.requests[-1]["body"]["messages"] == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hello"}]
        with anthropic_sdk(gateway, full_key["key"]).messages.stream(model="qwen-upstream", max_tokens=32, messages=[{"role": "user", "content": "hello"}]) as stream:
            assert "".join(stream.text_stream) == "hi"
            assert stream.get_final_message().usage.output_tokens == 2
        with pytest.raises(anthropic.NotFoundError) as unknown:
            anthropic_sdk(gateway, full_key["key"]).messages.create(model="gpt-4o", max_tokens=8, messages=[{"role": "user", "content": "hello"}])
        assert unknown.value.body["error"]["type"] == "not_found_error"
        # Counting and model listing are answered by the gateway itself, in the SDK's own shapes.
        counted = anthropic_sdk(gateway, full_key["key"]).messages.count_tokens(model="qwen-upstream", messages=[{"role": "user", "content": "hello"}])
        assert counted.input_tokens > 0
        assert "qwen-upstream" in [model.id for model in anthropic_sdk(gateway, full_key["key"]).models.list()]

    rows = logs(owned_engine)
    assert [(row.endpoint, row.stream, row.status_code, row.error_code) for row in rows] == [
        ("chat.completions", False, 200, None),
        ("chat.completions", False, 200, None),
        ("chat.completions", True, 200, None),
        ("embeddings", False, 200, None),
        ("chat.completions", False, 403, "model_not_allowed"),
        ("embeddings", False, 403, "model_not_allowed"),
        ("chat.completions", False, 404, "model_not_found"),
        ("chat.completions", False, 404, "model_not_found"),
        ("chat.completions", False, 503, "model_unavailable"),
        ("embeddings", False, 400, "model_endpoint_mismatch"),
        ("chat.completions", False, 200, None),
        ("chat.completions", False, 502, "upstream_error"),
        ("messages", False, 200, None),
        ("messages", True, 200, None),
        ("messages", False, 404, "model_not_found"),
    ]
    rescued_row, stranded_row = rows[-5:-3]
    assert [(row.upstream_model_id, row.total_tokens) for row in rows[-3:-1]] == [(chat_id, 5), (chat_id, 5)]
    # The call is counted under the deployment that answered; the trail keeps the one that did not.
    assert rescued_row.upstream_model_id == twin_id and rescued_row.total_tokens == 5
    assert [(item["model_id"], item["provider"], item["instance_name"], item["status_code"]) for item in rescued_row.attempts] == [
        (chat_id, "VLLM", "default", 503),
        (twin_id, "OpenAI-API-Compatible", "default", 200),
    ]
    assert (stranded_row.upstream_model_id, stranded_row.attempts) == (chat_id, None)
    first, twin, streamed, embedded, denied_row = rows[:5]
    assert (first.tenant_id, first.key_id, first.key_name, first.model, first.gateway_model_id) == (owner, full_key["id"], "eval-runner", "qwen-upstream", None)
    assert (first.upstream_model_id, first.upstream_model_name, first.end_user) == (chat_id, "qwen-upstream", "job-7")
    assert (first.prompt_tokens, first.completion_tokens, first.total_tokens, first.usage_source) == (3, 2, 5, "reported")
    assert (twin.model, twin.upstream_model_id) == ("OpenAI-API-Compatible/qwen-upstream", twin_id)
    assert (streamed.model, streamed.upstream_model_id, streamed.total_tokens, streamed.usage_source) == ("VLLM/default/qwen-upstream", chat_id, 5, "reported")
    assert streamed.first_token_ms is not None
    assert (embedded.prompt_tokens, embedded.completion_tokens, embedded.total_tokens) == (4, None, 4)
    assert (denied_row.key_name, denied_row.tags, denied_row.upstream_model_id, denied_row.total_tokens, denied_row.usage_source) == ("indexing-job", {"purpose": "index"}, None, None, "unavailable")
    assert all(row.started_at.tzinfo is not None and row.duration_ms >= 0 for row in rows)
    with Session(owned_engine) as reader:
        assert reader.get(GatewayKey, full_key["id"]).last_used_at is not None

    async def health_round() -> list[HealthResult]:
        engine = create_async_engine(owned_engine.url)
        try:
            store = SqlGatewayStore(async_sessionmaker(engine, expire_on_commit=False))
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as probe_client:
                return await HealthChecker(probe_client, store, Router(), interval_seconds=300.0).run_once()
        finally:
            await engine.dispose()

    # A health check probes every callable model of owners that have a usable key, with the smallest request.
    upstream.failing.add("twin.internal")
    probes_from = len(upstream.requests)
    checked = {result.model_id: result for result in _run(health_round()) if result.tenant_id == owner}
    upstream.failing.clear()
    assert {model_id: result.status for model_id, result in checked.items()} == {chat_id: "healthy", embed_id: "healthy", twin_id: "unhealthy"}
    assert (checked[twin_id].status_code, checked[chat_id].status_code, checked[twin_id].failure_class) == (503, 200, "transient")
    probes = {item["url"]: item["body"] for item in upstream.requests[probes_from:] if "models.internal" in item["url"]}
    assert probes["http://models.internal:8000/v1/chat/completions"] == {"model": "qwen-upstream", "messages": [{"role": "user", "content": "test"}], "max_tokens": 1}
    assert probes["http://models.internal:9997/v1/embeddings"] == {"model": "bge-upstream", "input": "test"}
    # Probes are not calls: they leave no request log.
    assert len(logs(owned_engine)) == len(rows)

    with management(owned_engine, owner) as admin:
        # With health checks off the management page shows no health at all.
        assert all(item["health"] is None for item in admin.get(f"{_PREFIX}/catalog").json()["data"]["models"])
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(get_app_config().model_gateway, "health_check_interval_seconds", 300.0)
            shown = {item["model_id"]: item["health"] for item in admin.get(f"{_PREFIX}/catalog").json()["data"]["models"]}
        assert (shown[chat_id]["status"], shown[twin_id]["status"], shown[twin_id]["status_code"], shown[closed_id]) == ("healthy", "unhealthy", 503, None)
        assert (shown[twin_id]["failure_class"], shown[chat_id]["failure_class"], shown[chat_id]["source"]) == ("transient", None, "probe")
        assert "overloaded" in shown[twin_id]["error_message"] and shown[chat_id]["checked_at"]
    # The next round replaces the result instead of adding one.
    _run(health_round())
    with Session(owned_engine) as reader:
        stored_health = reader.scalars(sa.select(GatewayModelHealth).where(GatewayModelHealth.tenant_id == owner)).all()
        assert {row.model_id: row.status for row in stored_health} == {chat_id: "healthy", embed_id: "healthy", twin_id: "healthy"}

    with management(owned_engine, owner) as admin:
        requests = {item["model_id"]: item["requests_7d"] for item in admin.get(f"{_PREFIX}/catalog").json()["data"]["models"]}
        assert requests == {chat_id: 5, closed_id: 0, embed_id: 1, hosted_id: 0, twin_id: 2}
        # Renaming a key leaves history under the name it had at call time.
        assert admin.patch(f"{_PREFIX}/keys/{full_key['id']}", json={"name": "eval-runner-2", "description": "nightly evals"}).status_code == 200
        key_logs = admin.get(f"{_PREFIX}/logs", params={"key_id": full_key["id"], "outcome": "success"}).json()["data"]
        assert key_logs["total"] == 7 and {item["key_name"] for item in key_logs["items"]} == {"eval-runner"}
        assert [len(item["attempts"] or []) for item in key_logs["items"]].count(2) == 1
        assert admin.get(f"{_PREFIX}/logs", params={"outcome": "error"}).json()["data"]["total"] == 8
        by_status = {code: admin.get(f"{_PREFIX}/logs", params={"status_code": code}).json()["data"]["total"] for code in (200, 299)}
        assert by_status[200] >= 4 and by_status[299] == 0
        by_model = {item["model"]: item["total_tokens"] for item in admin.get(f"{_PREFIX}/usage", params={"group_by": "model"}).json()["data"]["items"]}
        assert (by_model["qwen-upstream"], by_model["VLLM/default/qwen-upstream"], by_model["embed-default"]) == (20, 5, 4)

        # Closing a connection closes every model in it, for every key.
        closed_catalog = admin.patch(f"{_PREFIX}/catalog/instances/{vllm}", json={"enabled": False}).json()["data"]
        assert {item["model_id"]: item["state"] for item in closed_catalog["models"] if item["instance_id"] == vllm} == {chat_id: "closed", closed_id: "closed"}
        assert admin.patch(f"{_PREFIX}/catalog/instances/missing", json={"enabled": True}).status_code == 404
    with data_plane(owned_engine, httpx.MockTransport(upstream)) as gateway:
        assert sdk(gateway, connection_key["key"]).models.list().data == []
        with pytest.raises(openai.NotFoundError):
            sdk(gateway, full_key["key"]).chat.completions.create(model="VLLM/qwen-upstream", messages=[])
        # The shared name lives on in the connection that is still open.
        sdk(gateway, full_key["key"]).chat.completions.create(model="qwen-upstream", messages=hello)
        assert upstream.requests[-1]["url"] == "http://twin.internal:8000/v1/chat/completions"

    with management(owned_engine, owner) as admin:
        assert admin.patch(f"{_PREFIX}/catalog/instances/{vllm}", json={"enabled": True}).status_code == 200
        # A new default applies to models registered later; the ones that exist keep their state.
        assert admin.patch(f"{_PREFIX}/settings", json={"new_models_enabled": False}).json()["data"]["new_models_enabled"] is False
    with Session(owned_engine) as db:
        later_id = register_model(db, owner, "VLLM", "qwen-later", "chat", "http://models.internal:8000", "upstream-secret")
        db.commit()
    with management(owned_engine, owner) as admin:
        models = {item["model_id"]: item for item in admin.get(f"{_PREFIX}/catalog").json()["data"]["models"]}
        assert (models[hosted_id]["enabled"], models[hosted_id]["enabled_by_default"]) == (True, False)
        assert (models[chat_id]["state"], models[later_id]["state"], models[later_id]["enabled_by_default"]) == ("callable", "closed", True)
        # Switching back reports the new default, and the model registered meanwhile stays closed.
        reopened = admin.patch(f"{_PREFIX}/settings", json={"new_models_enabled": True}).json()["data"]
        later = next(item for item in reopened["models"] if item["model_id"] == later_id)
        assert (reopened["new_models_enabled"], later["state"], later["enabled_by_default"]) == (True, "closed", False)

        # Rotation gives the key a new secret; everything else about it stays.
        rotated = admin.post(f"{_PREFIX}/keys/{narrow_key['id']}/rotate", json={"grace_period_seconds": 3600}).json()["data"]
        assert (rotated["id"], rotated["name"], rotated["allowed_model_ids"]) == (narrow_key["id"], "indexing-job", [embed_id])
        assert rotated["key"] != narrow_key["key"] and rotated["rotated_at"] and rotated["previous_key_expires_at"]
        assert rotated["key"] not in json.dumps(admin.get(f"{_PREFIX}/keys").json()["data"])
        assert admin.post(f"{_PREFIX}/keys/{narrow_key['id']}/rotate", json={"grace_period_seconds": -1}).status_code in (400, 422)
        assert admin.post(f"{_PREFIX}/keys/missing/rotate", json={}).status_code == 404
    with data_plane(owned_engine, httpx.MockTransport(upstream)) as gateway:
        # During the grace period the old secret and the new one both work.
        for secret in (narrow_key["key"], rotated["key"]):
            assert [model.id for model in sdk(gateway, secret).models.list().data] == ["embed-default"]
    with management(owned_engine, owner) as admin:
        # Without a grace period every earlier secret stops at once, the one still in its grace period included.
        latest = admin.post(f"{_PREFIX}/keys/{narrow_key['id']}/rotate", json={}).json()["data"]
        assert latest["previous_key_expires_at"] is None
    with data_plane(owned_engine, httpx.MockTransport(upstream)) as gateway:
        for secret in (narrow_key["key"], rotated["key"]):
            with pytest.raises(openai.AuthenticationError) as replaced:
                sdk(gateway, secret).models.list()
            assert replaced.value.code == "invalid_api_key"
        assert [model.id for model in sdk(gateway, latest["key"]).models.list().data] == ["embed-default"]
    narrow_key = {**narrow_key, "key": latest["key"]}

    with management(owned_engine, owner) as admin:
        # Lifecycle: disable, revoke; a revoked key cannot be changed again.
        assert admin.patch(f"{_PREFIX}/keys/{narrow_key['id']}", json={"status": "disabled"}).json()["data"]["status"] == "disabled"
        assert admin.post(f"{_PREFIX}/keys/{empty_key['id']}/revoke").json()["data"]["status"] == "revoked"
        assert admin.patch(f"{_PREFIX}/keys/{empty_key['id']}", json={"status": "active"}).status_code == 409
        assert admin.post(f"{_PREFIX}/keys/{empty_key['id']}/rotate", json={}).status_code == 409
        assert admin.patch(f"{_PREFIX}/keys/missing", json={"name": "x"}).status_code == 404
    with Session(owned_engine) as db:
        # An expiry in the past cannot be set through the API; age the key directly.
        db.execute(sa.update(GatewayKey).where(GatewayKey.id == full_key["id"]).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        db.commit()

    with data_plane(owned_engine, httpx.MockTransport(upstream)) as gateway:
        for key, code in ((narrow_key["key"], "api_key_disabled"), (empty_key["key"], "api_key_disabled"), (full_key["key"], "api_key_expired")):
            with pytest.raises(openai.AuthenticationError) as refused:
                sdk(gateway, key).models.list()
            assert refused.value.code == code

    async def blocking_references(model_id: int) -> list[dict[str, Any]]:
        engine = create_async_engine(owned_engine.url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                rows = [row for row in await model_provider_service.models_for_owner(db, owner) if row[0].id == model_id]
                return [dict(item) for item in await collect_blocking_model_references(db, rows)]
        finally:
            await engine.dispose()

    # Being open in the gateway, or named in a key, does not keep a model from being deleted.
    assert _run(blocking_references(embed_id)) == []
    assert _run(blocking_references(chat_id)) == []


def test_gateway_limits_refuse_and_quota_survives_a_restart(owned_engine: Engine) -> None:
    owner = uuid.uuid4().hex
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        chat_id = register_model(db, owner, "VLLM", "qwen-upstream", "chat", "http://models.internal:8000", "upstream-secret")
        db.commit()
    upstream = _Upstream()
    prompt = [{"role": "user", "content": "hello"}]

    with management(owned_engine, owner) as admin:
        chat_model = admin.patch(f"{_PREFIX}/catalog/models/{chat_id}", json={"max_parallel_requests": 8}).json()["data"]
        assert chat_model["max_parallel_requests"] == 8 and chat_model["rpm_limit"] is None
        scope = {"allowed_model_ids": [chat_id]}
        rate_key = admin.post(f"{_PREFIX}/keys", json={"name": "rate-limited", "rpm_limit": 2, **scope}).json()["data"]
        token_key = admin.post(f"{_PREFIX}/keys", json={"name": "token-limited", "tpm_limit": 4, **scope}).json()["data"]
        quota_key = admin.post(f"{_PREFIX}/keys", json={"name": "quota-limited", "token_quota": 8, "quota_period": "day", **scope}).json()["data"]
        assert (rate_key["rpm_limit"], token_key["tpm_limit"], quota_key["token_quota"], quota_key["quota_period"], quota_key["quota_used"]) == (2, 4, 8, "day", 0)
        assert admin.post(f"{_PREFIX}/keys", json={"name": "half-quota", "token_quota": 8}).status_code == 422
    with Session(owned_engine) as reader:
        stored = reader.get(GatewayKey, quota_key["id"])
        assert stored is not None and (stored.token_quota, stored.quota_period, stored.rpm_limit) == (8, "day", None)

    # Each call uses 5 tokens. A fixed clock keeps every call inside one minute of today.
    fixed = datetime.now(UTC).replace(second=5, microsecond=0)
    with data_plane(owned_engine, httpx.MockTransport(upstream), clock=lambda: fixed) as gateway:
        rate, tokens, quota = sdk(gateway, rate_key["key"]), sdk(gateway, token_key["key"]), sdk(gateway, quota_key["key"])
        rate.chat.completions.create(model="qwen-upstream", messages=prompt)
        rate.chat.completions.create(model="qwen-upstream", messages=prompt)
        with pytest.raises(openai.RateLimitError) as too_fast:
            rate.chat.completions.create(model="qwen-upstream", messages=prompt)
        assert (too_fast.value.code, too_fast.value.type) == ("rate_limit_exceeded", "requests")
        assert too_fast.value.response.headers["retry-after"] == "55"

        tokens.chat.completions.create(model="qwen-upstream", messages=prompt)
        with pytest.raises(openai.RateLimitError) as too_many_tokens:
            tokens.chat.completions.create(model="qwen-upstream", messages=prompt)
        assert (too_many_tokens.value.code, too_many_tokens.value.type) == ("rate_limit_exceeded", "tokens")

        # Quota of 8: the second call crosses it and completes, the third is refused.
        quota.chat.completions.create(model="qwen-upstream", messages=prompt)
        list(quota.chat.completions.create(model="qwen-upstream", messages=prompt, stream=True))
        forwarded = len(upstream.requests)
        with pytest.raises(openai.RateLimitError) as spent:
            quota.chat.completions.create(model="qwen-upstream", messages=prompt)
        assert (spent.value.code, spent.value.type) == ("insufficient_quota", "insufficient_quota")
        assert len(upstream.requests) == forwarded == 5

    refused = [(row.key_name, row.status_code, row.error_code, row.total_tokens) for row in logs(owned_engine) if row.status_code == 429]
    assert sorted(refused) == [("quota-limited", 429, "insufficient_quota", None), ("rate-limited", 429, "rate_limit_exceeded", None), ("token-limited", 429, "rate_limit_exceeded", None)]

    # A new process starts with empty minute windows, but the quota is read back from the logs.
    with data_plane(owned_engine, httpx.MockTransport(upstream)) as restarted:
        with pytest.raises(openai.RateLimitError) as still_spent:
            sdk(restarted, quota_key["key"]).chat.completions.create(model="qwen-upstream", messages=prompt)
        assert still_spent.value.code == "insufficient_quota"
        assert len(upstream.requests) == forwarded
        sdk(restarted, rate_key["key"]).chat.completions.create(model="qwen-upstream", messages=prompt)

    with management(owned_engine, owner) as admin:
        by_name = {item["name"]: item for item in admin.get(f"{_PREFIX}/keys").json()["data"]}
        assert by_name["quota-limited"]["quota_used"] == 10
        assert by_name["rate-limited"]["quota_used"] is None
        # Raising the quota takes effect on the next request; clearing a limit uses explicit nulls.
        raised = admin.patch(f"{_PREFIX}/keys/{quota_key['id']}", json={"token_quota": 100, "quota_period": "day"}).json()["data"]
        assert (raised["token_quota"], raised["quota_used"]) == (100, 10)
        assert admin.patch(f"{_PREFIX}/keys/{quota_key['id']}", json={"token_quota": 100}).status_code == 422
        cleared = admin.patch(f"{_PREFIX}/keys/{rate_key['id']}", json={"rpm_limit": None}).json()["data"]
        assert cleared["rpm_limit"] is None
        assert admin.patch(f"{_PREFIX}/catalog/models/{chat_id}", json={"max_parallel_requests": None, "rpm_limit": 600}).json()["data"]["rpm_limit"] == 600

    with data_plane(owned_engine, httpx.MockTransport(upstream)) as gateway:
        completion = sdk(gateway, quota_key["key"]).chat.completions.create(model="qwen-upstream", messages=prompt)
        assert completion.usage is not None and completion.usage.total_tokens == 5

    with management(owned_engine, owner) as admin:
        overview = admin.get(f"{_PREFIX}/overview", params={"days": 1}).json()["data"]
        assert (overview["base_url"], overview["listen_port"], overview["bucket"], len(overview["series"])) == ("http://127.0.0.1:9390/v1", 9390, "hour", 24)
        assert overview["data_plane"] in {"up", "down"}
        assert overview["counts"] == {"providers": 1, "callable_models": 1, "active_keys": 3}
        totals = overview["totals"]
        assert (totals["requests"], totals["errors"], totals["total_tokens"], totals["usage_unknown"]) == (11, 4, 35, 4)
        assert totals["first_token_p50_ms"] is not None and overview["previous_requests"] == 0
        assert sum(item["requests"] for item in overview["series"]) == 11 and sum(item["errors"] for item in overview["series"]) == 4
        assert sum(item["total_tokens"] for item in overview["series"]) == totals["total_tokens"]
        assert overview["errors_by_code"] == [{"status_code": 429, "code": "insufficient_quota", "count": 2}, {"status_code": 429, "code": "rate_limit_exceeded", "count": 2}]
        assert overview["top_models"] == [{"model_id": chat_id, "name": "qwen-upstream", "provider": "VLLM", "requests": 11, "total_tokens": 35, "spend": 0.0}]
        # A self-hosted model has no listed price, so nothing was charged so far.
        assert totals["spend"] == 0
        assert {item["key_name"]: item["total_tokens"] for item in overview["top_keys"]} == {"rate-limited": 15, "token-limited": 5, "quota-limited": 15}
        assert [item["status_code"] for item in overview["recent_errors"]] == [429, 429, 429, 429]
        week = admin.get(f"{_PREFIX}/overview", params={"days": 7}).json()["data"]
        assert (week["bucket"], len(week["series"])) == ("day", 7)
        # Whole days come from the daily sums and agree with the logs of the same calls.
        assert {name: week["totals"][name] for name in ("requests", "errors", "total_tokens", "usage_unknown")} == {"requests": 11, "errors": 4, "total_tokens": 35, "usage_unknown": 4}
        assert week["series"][-1]["requests"] == 11 and week["top_models"] == overview["top_models"]
        assert {item["key_name"]: item["total_tokens"] for item in week["top_keys"]} == {"rate-limited": 15, "token-limited": 5, "quota-limited": 15}
        # Each bucket also splits its tokens by direction and its calls by model, and says how long answers took.
        for window in (overview, week):
            assert window["series_models"] == [{"model_id": chat_id, "name": "qwen-upstream", "provider": "VLLM"}]
            assert sum(item["prompt_tokens"] + item["completion_tokens"] for item in window["series"]) == window["totals"]["total_tokens"]
            assert sum(item["models"].get(chat_id, {"requests": 0})["requests"] for item in window["series"]) == 11
            assert sum(item["models"].get(chat_id, {"total_tokens": 0})["total_tokens"] for item in window["series"]) == 35
            busy = next(item for item in reversed(window["series"]) if item["requests"])
            assert busy["latency"]["p50_ms"] is not None and busy["latency"]["p50_ms"] <= busy["latency"]["p95_ms"]
            assert window["totals"]["duration_p50_ms"] is not None and window["totals"]["duration_p50_ms"] <= window["totals"]["duration_p95_ms"]
            assert all(item["latency"] is None for item in window["series"] if not item["requests"])

        # The operator prices the model; later calls carry their cost.
        assert admin.patch(f"{_PREFIX}/catalog/models/{chat_id}", json={"output_cost_per_token": "0.000002"}).status_code == 422
        priced = admin.patch(f"{_PREFIX}/catalog/models/{chat_id}", json={"input_cost_per_token": "0.000001", "output_cost_per_token": "0.000002"}).json()["data"]
        assert priced["pricing"] == {"source": "custom", "input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06, "cache_read_input_token_cost": None, "tiered": False}
        assert priced["custom_pricing"] == {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06, "cache_read_input_token_cost": None}

    with data_plane(owned_engine, httpx.MockTransport(upstream)) as gateway:
        sdk(gateway, quota_key["key"]).chat.completions.create(model="qwen-upstream", messages=prompt)

    last = logs(owned_engine)[-1]
    # 3 prompt tokens at 1e-6 and 2 completion tokens at 2e-6.
    assert (last.cost, last.cache_read_input_tokens) == (Decimal("0.000007"), None)
    with Session(owned_engine) as reader:
        days = reader.scalars(sa.select(GatewayDailyUsage)).all()
        assert sum(row.api_requests for row in days) == len(logs(owned_engine)) == sum(row.successful_requests + row.failed_requests for row in days)
        assert sum(row.spend for row in days) == Decimal("0.000007")
        assert sum(row.total_tokens for row in days) == sum(row.total_tokens or 0 for row in logs(owned_engine))

    with management(owned_engine, owner) as admin:
        for window in (1, 7):
            spent = admin.get(f"{_PREFIX}/overview", params={"days": window}).json()["data"]
            assert spent["totals"]["spend"] == pytest.approx(7e-06) and spent["top_models"][0]["spend"] == pytest.approx(7e-06)
            assert sum(item["spend"] for item in spent["series"]) == pytest.approx(7e-06)
        newest = admin.get(f"{_PREFIX}/logs", params={"page_size": 1}).json()["data"]["items"][0]
        assert newest["cost"] == pytest.approx(7e-06)
        # Clearing the price returns the model to the cost map, which does not list it.
        cleared_price = admin.patch(f"{_PREFIX}/catalog/models/{chat_id}", json={"input_cost_per_token": None, "output_cost_per_token": None}).json()["data"]
        assert cleared_price["pricing"] is None and cleared_price["custom_pricing"] is None

    # Retention deletes the oldest logs in batches; the daily sums stay.
    async_engine = create_async_engine(owned_engine.url)
    store = SqlGatewayStore(async_sessionmaker(async_engine, expire_on_commit=False))

    async def prune() -> tuple[int, int, int]:
        try:
            cutoff = datetime.now(UTC) + timedelta(minutes=1)
            return await store.delete_logs_before(cutoff - timedelta(days=1), 100), await store.delete_logs_before(cutoff, 5), await store.delete_logs_before(cutoff, 100)
        finally:
            await async_engine.dispose()

    total = len(logs(owned_engine))
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
        assert runner.run(prune()) == (0, 5, total - 5)
    assert logs(owned_engine) == []
    with Session(owned_engine) as reader:
        assert reader.scalar(sa.select(sa.func.sum(GatewayDailyUsage.api_requests))) == total
