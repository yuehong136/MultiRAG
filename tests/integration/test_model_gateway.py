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
from typing import Any

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

from api.db.db_models import GatewayKey, GatewayModel, GatewayRequestLog, TenantLLM
from api.db.services import model_provider_service
from api.db.services.model_deletion_policy import collect_blocking_model_references
from api.model_gateway.keys import hash_key
from tests.support.database import _alembic_config
from tests.support.model_gateway import MANAGEMENT_PREFIX as _PREFIX
from tests.support.model_gateway import data_plane, logs, management, register_model, sdk
from tests.support.provider_instances import owned_engine as owned_engine
from tests.support.provider_instances import tenant
from tests.support.resource_journal import ResourceJournal

pytestmark = pytest.mark.integration

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

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "authorization": request.headers.get("authorization"), "body": body})
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


def test_gateway_schema_matches_models_on_both_bootstrap_paths(owned_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """The revision creates the model schema, and accepts tables created model-first."""
    revision = ScriptDirectory.from_config(_alembic_config()).get_revision("d1f3a5c7e9b2")
    assert revision is not None and revision.down_revision == "c9e1f3a5b7d9"
    migration = revision.module
    journal = ResourceJournal("model_gateway_schema")
    for model_first in (False, True):
        schema = "model_gateway_" + uuid.uuid4().hex[:12]
        journal.event("reserve-schema", schema=schema, database=owned_engine.url.database, model_first=model_first)
        monkeypatch.setattr(migration, "SCHEMA", schema)
        with owned_engine.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            if model_first:
                metadata = sa.MetaData()
                for model in (GatewayModel, GatewayKey, GatewayRequestLog):
                    model.__table__.to_metadata(metadata, schema=schema)
                metadata.create_all(connection)
            migration.upgrade()
            migration.upgrade()
            inspector = sa.inspect(connection)
            for model in (GatewayModel, GatewayKey, GatewayRequestLog):
                table = model.__table__
                actual = {column["name"]: column for column in inspector.get_columns(table.name, schema=schema)}
                assert set(actual) == set(table.columns.keys())
                for column in table.columns:
                    assert actual[column.name]["type"].compile(dialect=connection.dialect) == column.type.compile(dialect=connection.dialect), column.name
                    assert actual[column.name]["nullable"] == column.nullable, column.name
                expected_indexes = {(tuple(column.name for column in index.columns), bool(index.unique)) for index in table.indexes}
                actual_indexes = {(tuple(index["column_names"]), bool(index["unique"])) for index in inspector.get_indexes(table.name, schema=schema) if not index.get("duplicates_constraint")}
                assert actual_indexes == expected_indexes, table.name
                assert {item["name"] for item in inspector.get_check_constraints(table.name, schema=schema)} == {item.name for item in table.constraints if isinstance(item, sa.CheckConstraint)}

            models, keys, _logs_table = migration._tables()
            connection.execute(keys.insert().values(id="key-1", tenant_id="t", name="runner", secret_hash="a" * 64, created_by="u"))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(keys.insert().values(id="key-2", tenant_id="t", name="runner", secret_hash="b" * 64, created_by="u"))
            # A revoked key frees its name, so rotation can keep the name.
            connection.execute(keys.update().where(keys.c.id == "key-1").values(status="revoked"))
            connection.execute(keys.insert().values(id="key-2", tenant_id="t", name="runner", secret_hash="b" * 64, created_by="u"))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(keys.update().where(keys.c.id == "key-2").values(status="paused"))
            connection.execute(models.insert().values(id="m-1", tenant_id="t", name="chat-default", model_id=1, model_type="chat", created_by="u"))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(models.insert().values(id="m-2", tenant_id="t", name="chat-default", model_id=2, model_type="chat", created_by="u"))
            with pytest.raises(RuntimeError, match="persisted model gateway data"):
                migration.downgrade()
            connection.execute(keys.delete())
            connection.execute(models.delete())
            migration.downgrade()
            assert not any(sa.inspect(connection).has_table(table.name, schema=schema) for table in migration._tables())
            connection.execute(sa.schema.DropSchema(schema))
        journal.event("schema-cleanup-readback", schema=schema, absent=True)


def test_gateway_serves_registered_models_with_scoped_keys(owned_engine: Engine) -> None:
    owner, other_owner = uuid.uuid4().hex, uuid.uuid4().hex
    with Session(owned_engine) as db:
        db.add_all([tenant(owner), tenant(other_owner)])
        chat_id = register_model(db, owner, "VLLM", "qwen-upstream___VLLM", "chat", "http://models.internal:8000", "upstream-secret")
        embed_id = register_model(db, owner, "Xinference", "bge-upstream", "embedding", "http://models.internal:9997/v1", "x")
        hosted_id = register_model(db, owner, "Anthropic", "claude-hosted", "chat", "https://api.anthropic.com", "hosted-secret")
        foreign_id = register_model(db, other_owner, "VLLM", "foreign-model", "chat", "http://models.internal:8001", "foreign-secret")
        db.commit()
    upstream = _Upstream()

    with management(owned_engine, owner) as admin:
        # Publishing: only relay-capable models of this owner, one public name each.
        published = admin.post(f"{_PREFIX}/models", json={"name": "chat-default", "model_id": chat_id, "model_type": "chat"})
        assert published.status_code == 200, published.text
        chat_model = published.json()["data"]
        assert chat_model["target"]["model_name"] == "qwen-upstream___VLLM" and chat_model["target"]["relayable"] is True
        embed_model = admin.post(f"{_PREFIX}/models", json={"name": "embed-default", "model_id": embed_id, "model_type": "embedding", "description": "bge"}).json()["data"]
        assert admin.post(f"{_PREFIX}/models", json={"name": "hosted", "model_id": hosted_id, "model_type": "chat"}).status_code == 400
        assert admin.post(f"{_PREFIX}/models", json={"name": "foreign", "model_id": foreign_id, "model_type": "chat"}).status_code == 400
        assert admin.post(f"{_PREFIX}/models", json={"name": "wrong-type", "model_id": chat_id, "model_type": "embedding"}).status_code == 400
        assert admin.post(f"{_PREFIX}/models", json={"name": "chat-default", "model_id": chat_id, "model_type": "chat"}).status_code == 409

        # Keys: nothing but a name is required; the plaintext is returned once.
        created = admin.post(f"{_PREFIX}/keys", json={"name": "eval-runner", "allowed_model_ids": [chat_model["id"], embed_model["id"]]})
        assert created.status_code == 200, created.text
        full_key = created.json()["data"]
        narrow_key = admin.post(f"{_PREFIX}/keys", json={"name": "indexing-job", "tags": {"purpose": "index"}, "allowed_model_ids": [embed_model["id"]]}).json()["data"]
        empty_key = admin.post(f"{_PREFIX}/keys", json={"name": "no-models"}).json()["data"]
        assert admin.post(f"{_PREFIX}/keys", json={"name": "bad-scope", "allowed_model_ids": ["missing"]}).status_code == 400
        assert admin.post(f"{_PREFIX}/keys", json={"name": "eval-runner"}).status_code == 409
        listed = admin.get(f"{_PREFIX}/keys").json()["data"]
        assert {item["name"] for item in listed} == {"eval-runner", "indexing-job", "no-models"}
        assert all("key" not in item and full_key["key"] not in json.dumps(item) for item in listed)
    with Session(owned_engine) as reader:
        stored = reader.get(GatewayKey, full_key["id"])
        assert stored is not None and stored.secret_hash == hash_key(full_key["key"])
        assert full_key["key"] not in json.dumps(stored.to_dict(), default=str)
        assert stored.description is None and stored.tags is None and stored.last_used_at is None

    with data_plane(owned_engine, httpx.MockTransport(upstream)) as gateway:
        client = sdk(gateway, full_key["key"])

        assert [model.id for model in client.models.list().data] == ["chat-default", "embed-default"]
        assert [model.id for model in sdk(gateway, narrow_key["key"]).models.list().data] == ["embed-default"]
        assert sdk(gateway, empty_key["key"]).models.list().data == []

        completion = client.chat.completions.create(model="chat-default", messages=[{"role": "user", "content": "hello"}], user="job-7")
        assert completion.choices[0].message.content == "hi"
        assert completion.usage is not None and completion.usage.total_tokens == 5
        assert upstream.requests[-1] == {
            "url": "http://models.internal:8000/v1/chat/completions",
            "authorization": "Bearer upstream-secret",
            "body": {"model": "qwen-upstream", "messages": [{"role": "user", "content": "hello"}], "user": "job-7"},
        }

        chunks = list(client.chat.completions.create(model="chat-default", messages=[{"role": "user", "content": "hello"}], stream=True))
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
            sdk(gateway, narrow_key["key"]).chat.completions.create(model="chat-default", messages=[])
        assert denied.value.code == "model_not_allowed"
        with pytest.raises(openai.NotFoundError) as missing:
            client.chat.completions.create(model="claude-hosted", messages=[])
        assert missing.value.code == "model_not_found"
        with pytest.raises(openai.AuthenticationError) as forged:
            sdk(gateway, full_key["key"][:-1] + ("A" if full_key["key"][-1] != "A" else "B")).models.list()
        assert forged.value.code == "invalid_api_key"
        assert len(upstream.requests) == forwarded

    rows = logs(owned_engine)
    assert [(row.endpoint, row.stream, row.status_code, row.error_code) for row in rows] == [
        ("chat.completions", False, 200, None),
        ("chat.completions", True, 200, None),
        ("embeddings", False, 200, None),
        ("chat.completions", False, 403, "model_not_allowed"),
        ("chat.completions", False, 404, "model_not_found"),
    ]
    first, streamed, embedded, denied_row, missing_row = rows
    assert (first.tenant_id, first.key_id, first.key_name, first.model, first.gateway_model_id) == (owner, full_key["id"], "eval-runner", "chat-default", chat_model["id"])
    assert (first.upstream_model_id, first.upstream_model_name, first.end_user) == (chat_id, "qwen-upstream", "job-7")
    assert (first.prompt_tokens, first.completion_tokens, first.total_tokens, first.usage_source) == (3, 2, 5, "reported")
    assert (streamed.total_tokens, streamed.usage_source) == (5, "reported") and streamed.first_token_ms is not None
    assert (embedded.prompt_tokens, embedded.completion_tokens, embedded.total_tokens) == (4, None, 4)
    assert (denied_row.key_name, denied_row.tags, denied_row.total_tokens, denied_row.usage_source) == ("indexing-job", {"purpose": "index"}, None, "unavailable")
    assert (missing_row.gateway_model_id, missing_row.upstream_model_id) == (None, None)
    assert all(row.started_at.tzinfo is not None and row.duration_ms >= 0 for row in rows)
    with Session(owned_engine) as reader:
        assert reader.get(GatewayKey, full_key["id"]).last_used_at is not None

    with management(owned_engine, owner) as admin:
        # Renaming a key leaves history under the name it had at call time.
        assert admin.patch(f"{_PREFIX}/keys/{full_key['id']}", json={"name": "eval-runner-2", "description": "nightly evals"}).status_code == 200
        key_logs = admin.get(f"{_PREFIX}/logs", params={"key_id": full_key["id"], "outcome": "success"}).json()["data"]
        assert key_logs["total"] == 3 and {item["key_name"] for item in key_logs["items"]} == {"eval-runner"}
        assert admin.get(f"{_PREFIX}/logs", params={"outcome": "error"}).json()["data"]["total"] == 2

        by_key = {item["key_id"]: item for item in admin.get(f"{_PREFIX}/usage", params={"group_by": "key"}).json()["data"]["items"]}
        assert by_key[full_key["id"]] == {
            "key_id": full_key["id"],
            "key_name": "eval-runner",
            "requests": 4,
            "errors": 1,
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "usage_unknown": 1,
        }
        assert (by_key[narrow_key["id"]]["requests"], by_key[narrow_key["id"]]["errors"], by_key[narrow_key["id"]]["usage_unknown"]) == (1, 1, 1)
        by_model = {item["model"]: item["total_tokens"] for item in admin.get(f"{_PREFIX}/usage", params={"group_by": "model"}).json()["data"]["items"]}
        assert by_model == {"chat-default": 10, "embed-default": 4, "claude-hosted": 0}
        assert sum(item["requests"] for item in admin.get(f"{_PREFIX}/usage", params={"group_by": "day"}).json()["data"]["items"]) == 5
        by_end_user = {item["end_user"]: item["requests"] for item in admin.get(f"{_PREFIX}/usage", params={"group_by": "end_user", "model": "chat-default"}).json()["data"]["items"]}
        assert by_end_user == {"job-7": 1, None: 2}

        # Lifecycle: disable, re-enable, revoke; a revoked key cannot be changed again.
        assert admin.patch(f"{_PREFIX}/keys/{narrow_key['id']}", json={"status": "disabled"}).json()["data"]["status"] == "disabled"
        assert admin.post(f"{_PREFIX}/keys/{empty_key['id']}/revoke").json()["data"]["status"] == "revoked"
        assert admin.patch(f"{_PREFIX}/keys/{empty_key['id']}", json={"status": "active"}).status_code == 409
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
    assert len(logs(owned_engine)) == 5

    async def blocking_references() -> list[dict[str, Any]]:
        engine = create_async_engine(owned_engine.url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                rows = [row for row in await model_provider_service.models_for_owner(db, owner) if row[0].id == chat_id]
                return [dict(item) for item in await collect_blocking_model_references(db, rows)]
        finally:
            await engine.dispose()

    def run(coroutine: Any) -> Any:
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
            return runner.run(coroutine)

    # A published name that is being served protects its model from deletion.
    assert run(blocking_references()) == [
        {"resource_type": "GatewayModel", "resource_id": chat_model["id"], "resource_name": "chat-default", "model_names": ["qwen-upstream___VLLM"], "is_deleted": False}
    ]
    with management(owned_engine, owner) as admin:
        assert admin.patch(f"{_PREFIX}/models/{chat_model['id']}", json={"status": "inactive"}).json()["data"]["status"] == "inactive"
    assert run(blocking_references()) == []
    with Session(owned_engine) as db:
        db.execute(sa.update(TenantLLM).where(TenantLLM.id == chat_id).values(status="0"))
        db.commit()
    with management(owned_engine, owner) as admin:
        # Re-enabling is a fresh publication and fails while the model is disabled.
        assert admin.patch(f"{_PREFIX}/models/{chat_model['id']}", json={"status": "active"}).status_code == 400
        assert admin.delete(f"{_PREFIX}/models/{embed_model['id']}").status_code == 200
        assert [item["name"] for item in admin.get(f"{_PREFIX}/models").json()["data"]] == ["chat-default"]
    with Session(owned_engine) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(GatewayRequestLog).where(GatewayRequestLog.gateway_model_id == embed_model["id"])) == 1
