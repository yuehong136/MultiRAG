"""Checked checkpoint storage and a renewable dataset writer lease.

The legacy search/delete APIs can hide backend failures. Checkpoint decisions
require complete Strong snapshots and writes that replace keys without deletion.
"""

import asyncio
import json
import threading
from collections.abc import Callable
from contextvars import ContextVar
from types import TracebackType
from typing import Any

from redis.lock import Lock

from api.db.services.task_service import has_canceled
from common import settings
from common.exceptions import TaskCanceledException
from common.misc_utils import thread_pool_exec
from core.utils.redis_conn import REDIS_CONN

current_writer: ContextVar["GraphRAGLease | None"] = ContextVar("graphrag_writer", default=None)

current_task: ContextVar[str] = ContextVar("graphrag_task", default="")

GRAPH_KINDS = ["graph", "subgraph", "entity", "relation", "community_report"]


async def settled_io(function: Callable[..., Any], *args: Any) -> Any:
    """Cancellation cannot stop a running executor thread; drain it before returning."""
    lease = current_writer.get()
    task_id = current_task.get()

    def invoke() -> Any:
        if lease is not None:
            lease.check()
            if task_id and has_canceled(task_id):
                raise TaskCanceledException(f"Task {task_id} was cancelled")
        return function(*args)

    task = asyncio.create_task(thread_pool_exec(invoke))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Retrieve an exception so shutdown never leaves an unobserved task.
        if task.done() and not task.cancelled():
            task.exception()
        raise


def graph_rows(index_name: str, kb_id: str, kinds: list[str], document_id: str | None = None) -> list[dict[str, Any]]:
    store = settings.docStoreConn
    if store.db_type() != "milvus":
        raise RuntimeError("GraphRAG checkpoints require checked atomic upsert storage; this backend is unsupported")
    if document_id is not None:
        if kinds != ["subgraph"]:
            raise ValueError("Document checkpoint lookup is only valid for subgraphs")
        rows = store.graph_subgraph_rows(index_name, kb_id, document_id)
    else:
        rows = store.graph_rows(index_name, kb_id, kinds)
    if any(row.get("kb_id") != kb_id or row.get("knowledge_graph_kwd") not in kinds or not row.get("pk") for row in rows):
        raise RuntimeError("Invalid GraphRAG snapshot scope")
    return rows


def delete_rows(index_name: str, kb_id: str, identifiers: list[str]) -> None:
    if identifiers:
        settings.docStoreConn.graph_delete_rows(index_name, kb_id, identifiers)


def validate_chunks(chunks: list[dict[str, Any]], index_name: str) -> None:
    """Reject destructive backends and lossy schema conversion before writing."""
    store = settings.docStoreConn
    # The capability guard must run before insert (Infinity insert deletes keys).
    if store.db_type() != "milvus":
        raise RuntimeError("GraphRAG checkpoints require checked atomic upsert storage")
    conn = store._get_connection()
    fields = {field["name"]: field for field in conn.describe_collection(index_name)["fields"]}
    for chunk in chunks:
        for name in ("content_with_weight", "source_id"):
            value = chunk.get(name, "")
            if isinstance(value, list):
                value = "\n".join(value)
            maximum = int(fields.get(name, {}).get("params", {}).get("max_length", 65535))
            if len(str(value).encode()) > maximum:
                raise ValueError(f"GraphRAG {name} exceeds the storage schema; checkpoint was not replaced")


def checked_insert(chunks: list[dict[str, Any]], index_name: str, kb_id: str) -> None:
    """Upsert and independently read back the actual stored checkpoint payload."""
    validate_chunks(chunks, index_name)
    store = settings.docStoreConn
    conn = store._get_connection()
    errors = store.insert(chunks, index_name, kb_id)
    if errors:
        raise RuntimeError(f"GraphRAG insert failed: {errors}")
    expression = f"kb_id == {json.dumps(kb_id)} && pk in {json.dumps([c['id'] for c in chunks])}"
    actual = {row["pk"]: row for row in conn.query(index_name, expr=expression, output_fields=["*"], consistency_level="Strong")}
    for chunk in chunks:
        row = actual.get(chunk["id"])
        if row is None or row.get("content_with_weight") != chunk["content_with_weight"] or row.get("knowledge_graph_kwd") != chunk["knowledge_graph_kwd"]:
            raise RuntimeError("GraphRAG insert readback mismatch")
        sources = row.get("source_id", [])
        if isinstance(sources, str):
            sources = sources.split("\n") if sources else []
        if sources != chunk.get("source_id", []):
            raise RuntimeError("GraphRAG source provenance readback mismatch")


def writer_key(tenant_id: str, kb_id: str) -> str:
    return f"graphrag:writer:{tenant_id}:{kb_id}"


def writer_active(tenant_id: str, kb_id: str) -> bool:
    return bool(REDIS_CONN.REDIS.exists(writer_key(tenant_id, kb_id)))


class GraphRAGLease:
    """One writer per tenant/dataset, including wipe; renew across long LLM phases."""

    def __init__(self, tenant_id: str, kb_id: str, *, wait: float = 30, ttl: int = 1200) -> None:
        self.key = writer_key(tenant_id, kb_id)
        self.lock = Lock(REDIS_CONN.REDIS, self.key, timeout=ttl, blocking_timeout=wait, thread_local=False)
        self.ttl = ttl
        self.stop = threading.Event()
        self.failure: BaseException | None = None
        self.renewal: threading.Thread | None = None

    def __enter__(self) -> "GraphRAGLease":
        if not self.lock.acquire():
            raise RuntimeError("GraphRAG writer is still active; retry after cancellation settles")
        self.renewal = threading.Thread(target=self._renew, daemon=True)
        self.renewal.start()
        return self

    def _renew(self) -> None:
        while not self.stop.wait(self.ttl / 3):
            try:
                self.lock.extend(self.ttl, replace_ttl=True)
            except BaseException as error:
                self.failure = error
                return

    def check(self) -> None:
        if self.failure or not self.lock.owned():
            raise RuntimeError("GraphRAG writer lease was lost") from self.failure

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        self.stop.set()
        if self.renewal:
            self.renewal.join(timeout=10)
        if self.lock.owned():
            self.lock.release()

    async def __aenter__(self) -> "GraphRAGLease":
        # Acquire must finish even if the requesting coroutine is cancelled.
        try:
            return await settled_io(self.__enter__)
        except BaseException:
            await settled_io(self.__exit__, None, None, None)
            raise

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        await settled_io(self.__exit__, exc_type, exc, traceback)


def control_key(kb_id: str) -> str:
    return f"graphrag:control:{kb_id}"


class GraphRAGControlLease(GraphRAGLease):
    """Serialize queue/pause/wipe binding changes independently of the worker."""

    def __init__(self, kb_id: str) -> None:
        super().__init__("", kb_id)
        self.key = control_key(kb_id)
        self.lock = Lock(REDIS_CONN.REDIS, self.key, timeout=self.ttl, blocking_timeout=30, thread_local=False)
