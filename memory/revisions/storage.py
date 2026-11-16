"""Scoped physical IO for managed publication; this module grants no authorization."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from common import settings
from common.asyncio_utils import finish_owned_work
from memory.revisions.domain import RevisionCapacityError, RevisionStorageUnknown, RevisionValidationError, positive_int
from memory.services.messages import MessageService, SourceMessageBindingError, index_name

_FIELDS = ["id", "message_id", "memory_id", "message_type", "source_id", "user_id", "agent_id", "session_id", "valid_at", "invalid_at", "forget_at", "status", "content", "content_embed"]
_MAX_PHYSICAL_ROWS = 10000


class _ReadPreparation(Protocol):
    def query(self, message_ids: list[int]) -> list[dict[str, Any]]: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class _ReadPass:
    owner: int
    backend: Any
    index: str
    memory_id: str
    preparation: _ReadPreparation | None


class ManagedMessageStore:
    """Pre-authorized one-bank adapter with strict reads and drained off-thread IO."""

    def __init__(self, tenant_id: str, memory_id: str) -> None:
        if not tenant_id or not memory_id:
            raise RevisionValidationError("memory_binding_required")
        self.tenant_id = tenant_id
        self.memory_id = memory_id
        self.index = index_name(tenant_id)
        self._read_pass: ContextVar[_ReadPass | None] = ContextVar("managed_read_pass", default=None)

    @asynccontextmanager
    async def prepared_read_pass(self) -> AsyncIterator[None]:
        """One lazy, read-only physical pass, isolated across nested async callers."""
        backend = settings.msgStoreConn
        prepare = getattr(backend, "prepare_source_reads", None)
        reader = prepare(_FIELDS.copy(), self.index, self.memory_id) if callable(prepare) else None
        scope = _ReadPass(id(self), backend, self.index, self.memory_id, reader)
        token = self._read_pass.set(scope)
        try:
            yield
        finally:
            try:
                if reader is not None:
                    await finish_owned_work(asyncio.to_thread(reader.close))
            finally:
                self._read_pass.reset(token)

    def _read_only_guard(self) -> None:
        if self._read_pass.get() is not None:
            raise RevisionValidationError("physical_read_pass_is_read_only")

    def _require(self, name: str) -> Any:
        method = getattr(settings.msgStoreConn, name, None)
        if not callable(method):
            raise RevisionValidationError("managed_backend_unsupported")
        return method

    def _decode(self, result: list[dict[str, Any]], *, backend: Any = None) -> list[dict[str, Any]]:
        decoder = settings.msgStoreConn if backend is None else backend
        messages = list(decoder.get_fields(result, _FIELDS.copy()).values())
        if len(messages) != len(result):
            raise SourceMessageBindingError("Physical read could not establish every message identity")
        seen: set[int] = set()
        for message in messages:
            identity = positive_int(message.get("message_id"), "invalid_physical_message_id")
            if message.get("memory_id") != self.memory_id or identity in seen or message.get("id") != f"{self.memory_id}_{identity}":
                raise SourceMessageBindingError("Physical read does not match the authorized memory")
            seen.add(identity)
        return messages

    async def read(self, message_id: int) -> dict[str, Any] | None:
        positive_int(message_id, "invalid_message_id")

        def retrieve() -> dict[str, Any] | None:
            scope = self._read_pass.get()
            if scope is None:
                result = self._require("query_source_rows")(_FIELDS.copy(), [message_id], self.index, self.memory_id)
                messages = self._decode(result)
            else:
                if (scope.owner, scope.index, scope.memory_id) != (id(self), self.index, self.memory_id) or settings.msgStoreConn is not scope.backend:
                    raise RevisionStorageUnknown("physical_read_backend_changed")
                if scope.preparation is not None:
                    result = scope.preparation.query([message_id])
                else:
                    query = getattr(scope.backend, "query_source_rows", None)
                    if not callable(query):
                        raise RevisionValidationError("managed_backend_unsupported")
                    result = query(_FIELDS.copy(), [message_id], self.index, self.memory_id)
                messages = self._decode(result, backend=scope.backend)
                if settings.msgStoreConn is not scope.backend:
                    raise RevisionStorageUnknown("physical_read_backend_changed")
            if len(messages) > 1 or (messages and messages[0]["message_id"] != message_id):
                raise SourceMessageBindingError("Physical read returned an unrequested message")
            return messages[0] if messages else None

        return await finish_owned_work(asyncio.to_thread(retrieve))

    async def write(self, message: dict[str, Any]) -> None:
        self._read_only_guard()
        identity = positive_int(message.get("message_id"), "invalid_message_id")
        if message.get("memory_id") != self.memory_id or type(message.get("status")) not in {bool, int} or message.get("status") not in (False, 0):
            raise RevisionValidationError("invalid_inactive_projection")
        value = {**message, "id": f"{self.memory_id}_{identity}"}

        def submit() -> None:
            self._require("query_source_rows")
            errors = settings.msgStoreConn.insert([value], self.index, self.memory_id)
            if errors:
                raise RevisionStorageUnknown("projection_write_unconfirmed")

        await finish_owned_work(asyncio.to_thread(submit))

    async def set_status(self, message_id: int, status: bool) -> None:
        self._read_only_guard()
        positive_int(message_id, "invalid_message_id")
        if type(status) is not bool:
            raise RevisionValidationError("invalid_status")

        def submit() -> None:
            query = self._require("query_source_rows")
            if not settings.msgStoreConn.update({"message_id": message_id}, {"status": status}, self.index, self.memory_id):
                raise RevisionStorageUnknown("projection_status_unconfirmed")
            rows = self._decode(query(_FIELDS.copy(), [message_id], self.index, self.memory_id))
            if len(rows) != 1 or rows[0]["message_id"] != message_id or rows[0].get("status") not in ((True, 1, "1") if status else (False, 0, "0")):
                raise RevisionStorageUnknown("projection_status_readback_mismatch")

        await finish_owned_work(asyncio.to_thread(submit))

    async def measure_bytes(self) -> int:
        def measure() -> int:
            return sum(MessageService.calculate_message_size(message) for message in self._inventory())

        return await finish_owned_work(asyncio.to_thread(measure))

    def _inventory(self) -> list[dict[str, Any]]:
        rows = self._require("query_management_rows")(_FIELDS.copy(), self.index, self.memory_id, _MAX_PHYSICAL_ROWS)
        if len(rows) >= _MAX_PHYSICAL_ROWS:
            raise RevisionCapacityError("physical_inventory_incomplete")
        return self._decode(rows)

    async def inventory(self) -> list[dict[str, Any]]:
        return await finish_owned_work(asyncio.to_thread(self._inventory))

    async def ensure_index(self, memory_id: str, dimension: int) -> None:
        self._read_only_guard()
        if memory_id != self.memory_id:
            raise RevisionValidationError("memory_binding_mismatch")
        positive_int(dimension, "invalid_embedding_dimension")
        await finish_owned_work(asyncio.to_thread(self._require("ensure_memory_index"), self.index, self.memory_id, dimension))

    async def source_dependents(self, source_id: int) -> list[int]:
        positive_int(source_id, "invalid_source_id")

        def inventory() -> list[int]:
            rows = self._require("query_management_rows")(_FIELDS.copy(), self.index, self.memory_id, _MAX_PHYSICAL_ROWS)
            if len(rows) >= _MAX_PHYSICAL_ROWS:
                raise RevisionCapacityError("physical_inventory_incomplete")
            return [message["message_id"] for message in self._decode(rows) if message["message_id"] == source_id or message.get("source_id") in (source_id, str(source_id))]

        return await finish_owned_work(asyncio.to_thread(inventory))

    async def delete(self, message_ids: list[int]) -> None:
        self._read_only_guard()
        if len(message_ids) > 100:
            raise RevisionValidationError("physical_delete_batch_too_large")
        ids = list(dict.fromkeys(positive_int(value, "invalid_message_id") for value in message_ids))
        if not ids:
            return

        def submit() -> None:
            query = self._require("query_source_rows")
            settings.msgStoreConn.delete({"message_id": ids}, self.index, self.memory_id)
            if self._decode(query(_FIELDS.copy(), ids, self.index, self.memory_id)):
                raise RevisionStorageUnknown("projection_delete_unconfirmed")

        await finish_owned_work(asyncio.to_thread(submit))

    async def drop(self) -> None:
        self._read_only_guard()

        def submit() -> None:
            exists = self._require("index_exists_strict")
            if exists(self.index, self.memory_id):
                settings.msgStoreConn.delete_idx(self.index, self.memory_id)
            if exists(self.index, self.memory_id):
                raise RevisionStorageUnknown("memory_drop_unconfirmed")

        await finish_owned_work(asyncio.to_thread(submit))
