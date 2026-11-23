"""Owned real Milvus/SQL inventory and transparent startup read observations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import Any

import pytest
import sqlalchemy as sa
from pymilvus import connections
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from api.apps.services import memory_startup_service, memory_verification_service
from api.db.db_models import Base, Memory, Task, TenantModel, TenantModelProvider
from api.db.memory_revision_models import MemoryEntry, MemoryEntryRevision, MemoryRevisionSource
from api.db.memory_verification_models import MemoryVerificationHead, MemoryVerificationOperation, MemoryVerificationRecord, MemoryVerificationSource
from common import settings
from common.token_utils import encoder
from memory.revisions.domain import content_hash
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import MessageService, index_name
from tests.support.memory_writes_http import api_request, manage_bank, result

SOURCE_ID = 9007199254770001
MESSAGE_BASE = 9007199254780000
SCOPE = {"user_id": "startup-milvus-scale-subject", "agent_id": "existing-agent", "session_id": None}
VERIFICATION_TABLES = (MemoryVerificationSource, MemoryVerificationOperation, MemoryVerificationHead, MemoryVerificationRecord)


def seed_inventory(env: dict[str, Any], *, size: int, confirmed: int, vector_size: int = 2, source_count: int = 1, long_text: bool = False) -> list[str]:
    """Author synthetic inventory; real verification reads bind actual Milvus rows."""
    if not 1 <= size <= 130 or not 0 <= confirmed <= size or vector_size not in (2, 1536) or not 1 <= source_count <= 3:
        raise ValueError("invalid_milvus_scale_inventory")
    mid, tenant, journal = env["memory_id"], env["tenants"]["personal"], env["journal"]
    scope = {**SCOPE, "session_id": "scale-session"}
    identities = [f"{1000 + index:032x}" for index in range(size)]
    journal.event(
        "reserve-startup-scale-inventory",
        memory_id=mid,
        tenant_id=tenant,
        source_id=SOURCE_ID,
        source_ids=[SOURCE_ID + index for index in range(source_count)],
        vector_size=vector_size,
        entry_ids=identities,
        message_ids=[MESSAGE_BASE + index for index in range(size)],
        verification_tables=[model.__tablename__ for model in VERIFICATION_TABLES],
    )
    raw = {
        "id": f"{mid}_{SOURCE_ID}",
        "memory_id": mid,
        "message_id": SOURCE_ID,
        "message_type": "raw",
        "source_id": 0,
        "content": "原始演练依据🧭：规则只用于隔离内网，不能批准生产访问。",
        "content_embed": [1.0, *([0.0] * (vector_size - 1))],
        "status": True,
        "valid_at": None,
        "invalid_at": None,
        "forget_at": None,
        **scope,
    }
    if long_text:
        raw["content"] += "\n" + "隔离环境的记录保留完整上下文🧭；范围只限合成演练，不授予生产访问或修改权限。\n" * 22
    sources = [
        {**raw, "id": f"{mid}_{SOURCE_ID + index}", "message_id": SOURCE_ID + index, "content": raw["content"] + (f"\n依据编号{index}" if source_count > 1 else "")} for index in range(source_count)
    ]
    assert MessageService.insert_message(sources, tenant, mid) == []
    messages = [
        {
            **raw,
            "id": f"{mid}_{MESSAGE_BASE + index}",
            "message_id": MESSAGE_BASE + index,
            "source_id": SOURCE_ID,
            "message_type": "semantic",
            "content": f"演练规则{index:03d}：仅隔离内网；不能用于生产审批；保留完整适用条件。",
        }
        for index in range(size)
    ]
    if long_text:
        for message in messages:
            message["content"] += "\n" + "完整适用条件：只处理隔离网内的合成数据；如来源或版本改变，先重新核验，不能据此批准生产操作。\n" * 20
    assert MessageService.insert_message(messages, tenant, mid) == []
    manage_bank(env)
    for index, source in enumerate(sources):
        registered = result(
            api_request(
                env,
                "POST",
                f"/memories/{mid}/sources/{source['message_id']}/adoptions",
                json={"operation_id": f"scale-source-registration-{index}", "expected_content_sha256": content_hash(source["content"])},
            )
        )
        assert registered["index_sync_status"] == "ready"
    template = asyncio.run(ManagedMessageStore(tenant, mid).read(MESSAGE_BASE))
    assert template is not None and template["content"] == messages[0]["content"]
    with Session(env["engine"]) as db:
        for entry_id, message in zip(identities, messages, strict=True):
            db.add(
                MemoryEntry(
                    entry_id=entry_id,
                    memory_id=mid,
                    tenant_id=tenant,
                    legacy_message_id=message["message_id"],
                    current_revision=1,
                    next_revision=2,
                    current_message_id=message["message_id"],
                    generation=1,
                    state="active",
                )
            )
        db.flush()
        for entry_id, message in zip(identities, messages, strict=True):
            snapshot = {**template, "id": message["id"], "message_id": message["message_id"], "content": message["content"]}
            db.add(
                MemoryEntryRevision(
                    entry_id=entry_id,
                    revision=1,
                    memory_id=mid,
                    tenant_id=tenant,
                    message_id=message["message_id"],
                    parent_revision=None,
                    origin="legacy_adoption",
                    content=message["content"],
                    content_sha256=content_hash(message["content"]),
                    assessment={"kind": "hypothesis", "verification_status": "unverified"},
                    message_snapshot=snapshot,
                )
            )
        db.flush()
        for entry_id in identities:
            for source in sources:
                db.add(
                    MemoryRevisionSource(
                        entry_id=entry_id,
                        revision=1,
                        memory_id=mid,
                        source_message_id=source["message_id"],
                        source_revision=1,
                        source_generation=1,
                        content_sha256=content_hash(source["content"]),
                        relation="context",
                        locator={"scope": scope},
                    )
                )
        db.commit()
    for index, entry_id in enumerate(identities[:confirmed]):
        prefix = f"/memories/{mid}/entries/{entry_id}"
        snapshot = result(api_request(env, "GET", prefix + "/verification-snapshot"))
        observed = result(
            api_request(
                env,
                "POST",
                prefix + "/verifications",
                json={
                    "operation_id": f"scale-human-{index}",
                    "expected_head_version": snapshot["head_version"],
                    "expected_binding_sha256": snapshot["binding_sha256"],
                    "result": "human_confirmed",
                    "method": "source_comparison",
                    "applicability": "仅隔离演练内网，不能用于生产审批。",
                    "notes": "Controlled synthetic manual assertion; not objective fact verification.",
                    "evidence": [],
                },
            )
        )
        assert observed["record"]["valid_for_current"] is True
    env["scale_entry_ids"], env["scale_messages"], env["scale_sources"] = identities, messages, sources
    return identities


def cleanup_verifications(env: dict[str, Any]) -> None:
    """Run before the parent fixture removes its owned entries and collection."""
    with Session(env["engine"]) as db:
        for model in VERIFICATION_TABLES:
            db.execute(sa.delete(model).where(model.memory_id == env["memory_id"]))
        db.commit()
    with Session(env["engine"]) as reader:
        remaining = {model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(model.memory_id == env["memory_id"])) for model in VERIFICATION_TABLES}
    assert not any(remaining.values()), remaining
    env["journal"].event("startup-scale-verification-cleanup-readback", memory_id=env["memory_id"], remaining=remaining)


class _RPC:
    """Count actual gRPC stub dispatches, preserving call/future behavior."""

    def __init__(self, original: Any, method: str, collection: str, record: Callable[[str, Any], None]) -> None:
        self.original, self.method, self.collection, self.record = original, method, collection, record

    def _observe(self, request: Any) -> None:
        if getattr(request, "collection_name", None) == self.collection:
            self.record(self.method, request)

    def __call__(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        self._observe(request)
        return self.original(request, *args, **kwargs)

    def future(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        self._observe(request)
        return self.original.future(request, *args, **kwargs)

    def with_call(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        self._observe(request)
        return self.original.with_call(request, *args, **kwargs)


@contextmanager
def observe_reads(env: dict[str, Any], patch: pytest.MonkeyPatch, *, concurrent: bool = False) -> Iterator[dict[str, Any]]:
    """Observe real SQL and same-collection wire RPCs; never replace read results."""
    database = env["engine"].url.database
    collection = index_name(env["tenants"]["personal"]) + "_" + env["memory_id"]
    observation: dict[str, Any] = {"sql": [], "rpc": [], "physical_reads": 0, "sql_transaction_at_io": [], "collection": collection}
    active: set[int] = set()
    lock = threading.RLock()

    def relevant(connection: Any) -> bool:
        return connection.engine.url.database == database

    def begin(connection: Any) -> None:
        if relevant(connection):
            with lock:
                active.add(id(connection))

    def end(connection: Any) -> None:
        with lock:
            active.discard(id(connection))

    def sql(connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if relevant(connection):
            with lock:
                observation["sql"].append(statement)

    def rpc(method: str, request: Any) -> None:
        with lock:
            observation["rpc"].append(
                {
                    "method": method,
                    "consistency_level": getattr(request, "consistency_level", None),
                    "limit": next((pair.value for pair in getattr(request, "query_params", []) if pair.key == "limit"), None),
                }
            )

    original_factory = memory_startup_service.verifications

    async def factory(*args: Any, **kwargs: Any) -> Any:
        service: Any = await original_factory(*args, **kwargs)
        original_read = service.storage.read

        async def read(identity: int) -> dict[str, Any] | None:
            assert not service.db.in_transaction(), "Startup SQL session must be released before physical IO"
            with lock:
                observation["physical_reads"] += 1
                observation["sql_transaction_at_io"].append(len(active))
                if not concurrent:
                    assert not active, "A scoped SQL connection remained in a transaction across physical IO"
            return await original_read(identity)

        patch.setattr(service.storage, "read", read)
        return service

    for name, callback in (("begin", begin), ("commit", end), ("rollback", end), ("before_cursor_execute", sql)):
        sa.event.listen(Engine, name, callback)
    handler = connections._fetch_handler(settings.msgStoreConn._using)
    try:
        for name in ("Query", "HasCollection", "DescribeCollection", "LoadCollection", "GetLoadingProgress", "GetLoadState"):
            original = getattr(handler._stub, name, None)
            if original is not None:
                patch.setattr(handler._stub, name, _RPC(original, name, collection, rpc))
        patch.setattr(memory_verification_service, "verifications", factory)
        patch.setattr(memory_startup_service, "verifications", factory)
        started = perf_counter()
        try:
            yield observation
        finally:
            observation["elapsed_seconds"] = perf_counter() - started
    finally:
        for name, callback in (("begin", begin), ("commit", end), ("rollback", end), ("before_cursor_execute", sql)):
            sa.event.remove(Engine, name, callback)


def preview(env: dict[str, Any], **overrides: Any) -> Any:
    return api_request(env, "POST", f"/memories/{env['memory_id']}/startup-context", json={**SCOPE, "budget_tokens": 32768, "max_items": 16, "inventory_limit": 128, **overrides})


def observation_record(env: dict[str, Any], observation: dict[str, Any], packet: dict[str, Any], *, label: str) -> dict[str, Any]:
    verbs = [statement.strip().split(None, 1)[0].upper() for statement in observation["sql"]]
    writes = [verb for verb in verbs if verb in {"INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "ALTER", "DROP", "TRUNCATE"}]
    assert not writes, writes
    assert observation["physical_reads"] == packet["selection"]["physical_reads"] <= 256
    queries = [row for row in observation["rpc"] if row["method"] == "Query"]
    assert len(queries) == observation["physical_reads"]
    assert all(row["consistency_level"] == 0 for row in queries), "Strict reads must retain Milvus Strong consistency"
    assert all(value == 0 for value in observation["sql_transaction_at_io"])
    encoded = json.dumps(packet, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    measured = len(encoder.encode(encoded, disallowed_special=()))
    assert measured <= packet["usage"]["estimated_tokens"] <= packet["usage"]["budget_tokens"]
    record = {
        "label": label,
        "sql_statement_count": len(verbs),
        "sql_verbs": dict(Counter(verbs)),
        "rpc_counts": dict(Counter(row["method"] for row in observation["rpc"])),
        "rpc_layer": "Selected MilvusServiceStub methods, not every transport operation",
        "observed_rpc_methods": ["Query", "HasCollection", "DescribeCollection", "LoadCollection", "GetLoadingProgress", "GetLoadState"],
        "runtime_versions": env["startup_runtime_versions"],
        "physical_read_attempts": observation["physical_reads"],
        "sql_transactions_at_io": observation["sql_transaction_at_io"],
        "elapsed_seconds": observation["elapsed_seconds"],
        "selection": packet["selection"],
        "omission_counts": packet["omitted"]["counts"],
        "included_items": len(packet["items"]),
        "estimated_tokens": packet["usage"]["estimated_tokens"],
        "measured_tokens": measured,
        "physical_backend": "real Milvus owned collection",
        "sql_backend": "real PostgreSQL scratch",
        "production_sla": False,
    }
    env["journal"].event("startup-milvus-scale-observation", observation=record, sql_statements=observation["sql"], rpc_calls=observation["rpc"])
    return record


def fingerprint(env: dict[str, Any]) -> str:
    """Independent exact-bank SQL readback, including task and model counters."""
    state: dict[str, Any] = {}
    with Session(env["engine"]) as reader:
        for table in Base.metadata.sorted_tables:
            if table.schema == "usr_ai" and "memory_id" in table.c:
                rows = reader.execute(sa.select(table).where(table.c.memory_id == env["memory_id"])).mappings()
                state[table.fullname] = sorted(json.dumps(dict(row), sort_keys=True, default=str) for row in rows)
        for model, predicate in (
            (Memory, Memory.id == env["memory_id"]),
            (Task, Task.doc_id == env["memory_id"]),
            (TenantModel, TenantModel.provider_id.in_(sa.select(TenantModelProvider.id).where(TenantModelProvider.tenant_id == env["tenants"]["personal"]))),
        ):
            rows = reader.execute(sa.select(model.__table__).where(predicate)).mappings()
            state[model.__tablename__] = sorted(json.dumps(dict(row), sort_keys=True, default=str) for row in rows)
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
