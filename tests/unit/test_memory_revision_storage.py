"""Managed physical IO is scoped, strict, and confirms mutation outcomes."""

from copy import deepcopy
from typing import Any

import pytest

from common import settings
from memory.revisions import storage
from memory.revisions.domain import RevisionCapacityError, RevisionStorageUnknown, RevisionValidationError
from memory.services.messages import MessageService, SourceMessageBindingError


def message(identity: int = 1, **changes: Any) -> dict[str, Any]:
    return {
        "id": f"bank_{identity}",
        "memory_id": "bank",
        "message_id": identity,
        "message_type": "raw",
        "source_id": 0,
        "content": "source",
        "content_embed": [1.0, 0.0],
        "status": True,
        "forget_at": None,
        **changes,
    }


class Physical:
    def __init__(self) -> None:
        self.rows = {1: message()}
        self.failure: Exception | None = None
        self.drop_status = False
        self.insert_count = 0

    def query_source_rows(self, fields: list[str], ids: list[int], index: str, bank: str) -> list[dict[str, Any]]:
        assert index == "memory_tenant" and bank == "bank"
        if self.failure is not None:
            raise self.failure
        return deepcopy([self.rows[identity] for identity in ids if identity in self.rows])

    def query_management_rows(self, fields: list[str], index: str, bank: str, limit: int) -> list[dict[str, Any]]:
        assert index == "memory_tenant" and bank == "bank" and limit == 10000
        return deepcopy(list(self.rows.values()))

    def get_fields(self, values: list[dict[str, Any]], fields: list[str]) -> dict[str, dict[str, Any]]:
        return {str(index): value for index, value in enumerate(values)}

    def insert(self, values: list[dict[str, Any]], index: str, bank: str) -> list[str]:
        self.insert_count += 1
        if self.failure is not None:
            raise self.failure
        for value in values:
            self.rows[value["message_id"]] = deepcopy(value)
        return []

    def update(self, condition: dict[str, Any], update: dict[str, Any], index: str, bank: str) -> bool:
        if not self.drop_status:
            self.rows[condition["message_id"]].update(update)
        return True

    def delete(self, condition: dict[str, Any], index: str, bank: str) -> None:
        for identity in condition["message_id"]:
            self.rows.pop(identity, None)


@pytest.fixture
def physical(monkeypatch: pytest.MonkeyPatch) -> Physical:
    value = Physical()
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", value)
    return value


async def test_storage_failure_is_not_a_missing_message(physical: Physical) -> None:
    physical.failure = TimeoutError("query failed")
    with pytest.raises(TimeoutError, match="query failed"):
        await storage.ManagedMessageStore("tenant", "bank").read(1)


async def test_storage_rejects_foreign_physical_identity(physical: Physical) -> None:
    physical.rows[1]["id"] = "foreign_1"
    with pytest.raises(SourceMessageBindingError, match="authorized memory"):
        await storage.ManagedMessageStore("tenant", "bank").read(1)


async def test_inventory_counts_inactive_forgotten_vectors_and_rejects_incomplete(physical: Physical, monkeypatch: pytest.MonkeyPatch) -> None:
    physical.rows[1].update(status=False, forget_at="2026-11-15")
    physical.rows[2] = message(2, source_id=1, message_type="semantic")
    adapter = storage.ManagedMessageStore("tenant", "bank")
    assert await adapter.measure_bytes() == sum(MessageService.calculate_message_size(value) for value in physical.rows.values())
    assert await adapter.source_dependents(1) == [1, 2]
    monkeypatch.setattr(storage, "_MAX_PHYSICAL_ROWS", 2)
    monkeypatch.setattr(physical, "query_management_rows", lambda *args: list(physical.rows.values()))
    with pytest.raises(RevisionCapacityError, match="incomplete"):
        await adapter.measure_bytes()


async def test_projection_requires_inactive_write_and_does_not_retry_unknown(physical: Physical) -> None:
    adapter = storage.ManagedMessageStore("tenant", "bank")
    with pytest.raises(RevisionValidationError, match="inactive"):
        await adapter.write(message())
    physical.failure = TimeoutError("unknown submission")
    with pytest.raises(TimeoutError, match="unknown submission"):
        await adapter.write(message(2, status=False))
    assert physical.insert_count == 1


async def test_status_ack_requires_independent_readback(physical: Physical) -> None:
    physical.drop_status = True
    with pytest.raises(RevisionStorageUnknown, match="readback"):
        await storage.ManagedMessageStore("tenant", "bank").set_status(1, False)


async def test_delete_confirms_absence(physical: Physical) -> None:
    adapter = storage.ManagedMessageStore("tenant", "bank")
    await adapter.delete([1])
    assert await adapter.read(1) is None


async def test_unsupported_storage_never_reports_managed_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", object())
    with pytest.raises(RevisionValidationError, match="unsupported"):
        await storage.ManagedMessageStore("tenant", "bank").measure_bytes()
