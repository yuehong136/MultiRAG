"""Source decisions release SQL before physical IO and fail closed on uncertainty."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from api.db.memory_revision_models import MemoryMutationOperation, MemoryPublicationOutbox, MemorySourceState
from memory.revisions.domain import RevisionConflict, content_hash
from memory.revisions.sources import SourceService, _raw_snapshot


class Session:
    def __init__(self) -> None:
        self.new: set[Any] = set()
        self.dirty: set[Any] = set()
        self.deleted: set[Any] = set()
        self.transaction = False
        self.expired = False
        self.rollbacks = self.commits = 0

    def in_transaction(self) -> bool:
        return self.transaction

    def expire_all(self) -> None:
        self.expired = True

    async def begin(self) -> None:
        self.transaction = True

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1
        self.transaction = False

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.transaction = False


def service(db: Session, reader: Any = None) -> SourceService:
    return SourceService(db, tenant_id="tenant", actor_id="actor", authorize=AsyncMock(), message_reader=reader or AsyncMock())  # type: ignore[arg-type]


async def test_absent_source_get_does_not_adopt_or_hold_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Session()

    async def read(memory_id: str, identity: int) -> dict[str, Any]:
        assert db.expired and not db.in_transaction() and db.rollbacks == 1
        return {
            "memory_id": memory_id,
            "message_id": identity,
            "message_type": "raw",
            "source_id": 0,
            "user_id": "",
            "agent_id": "agent",
            "session_id": "session",
            "content": "original",
            "status": True,
        }

    svc = service(db, read)
    monkeypatch.setattr(svc, "_memory", AsyncMock())
    monkeypatch.setattr(svc, "_source", AsyncMock(return_value=None))
    result = await svc.get_source_state("bank", 1)
    assert result["managed"] is False and result["source_revision"] is None
    assert result["content_sha256"] == content_hash("original") and db.commits == 0


@pytest.mark.parametrize("parent", [True, 1, "1"])
def test_raw_snapshot_cannot_adopt_a_derived_or_malformed_anchor(parent: Any) -> None:
    with pytest.raises(RevisionConflict, match="raw_source"):
        _raw_snapshot("bank", 1, {"message_id": 1, "message_type": "raw", "source_id": parent})


async def test_yield_integrity_conflict_rolls_back_instead_of_500() -> None:
    db = Session()
    with pytest.raises(RevisionConflict, match="concurrent_source"):
        async with service(db)._write("operation"):
            raise IntegrityError("INSERT", {}, ValueError("unique conflict"))
    assert db.rollbacks == 1 and db.commits == 0


async def test_managed_whole_delete_is_rejected_before_sql_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Session()
    svc = service(db)
    monkeypatch.setattr(svc, "_bank", AsyncMock(return_value=SimpleNamespace(state="active")))
    with pytest.raises(RevisionConflict, match="writer_coordination"):
        await svc.delete_memory("bank", operation_id="delete")
    assert db.commits == 0 and db.rollbacks == 1


async def test_raw_legacy_forget_uses_same_idempotency_digest_on_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Session()
    raw = {"memory_id": "bank", "message_id": 1, "message_type": "raw", "source_id": 0, "user_id": "", "agent_id": "agent", "session_id": "session", "content": "original", "status": True}
    svc = service(db, AsyncMock(return_value=raw))
    monkeypatch.setattr(svc, "_replay", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "get_source_state", AsyncMock(return_value={"managed": True, "source_revision": 1}))
    withdrawal = AsyncMock(return_value={"status": "published"})
    monkeypatch.setattr(svc, "withdraw_source", withdrawal)
    await svc.change_physical_state("bank", 1, operation_id="forget", forget=True)
    digest = svc._replay.call_args.args[2]  # type: ignore[attr-defined]
    assert withdrawal.call_args.kwargs["_request_sha256"] == digest


async def test_legacy_raw_forget_registers_and_tombstones_under_one_public_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Session()
    created: list[Any] = []
    db.add = created.append  # type: ignore[attr-defined]
    db.scalars = AsyncMock(return_value=[])  # type: ignore[attr-defined]
    raw = {"memory_id": "bank", "message_id": 1, "message_type": "raw", "source_id": 0, "user_id": "", "agent_id": "agent", "session_id": "session", "content": "original", "status": True}
    svc = service(db, AsyncMock(return_value=raw))
    monkeypatch.setattr(svc, "_replay", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "get_source_state", AsyncMock(return_value={"managed": False, "source_revision": None}))
    monkeypatch.setattr(svc, "adopt_source", AsyncMock(side_effect=AssertionError("Auxiliary source adoption would lose the public recovery handle")))
    monkeypatch.setattr(svc, "_bank", AsyncMock(return_value=SimpleNamespace(generation=1)))
    monkeypatch.setattr(svc, "_operation", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_source", AsyncMock(return_value=None))

    async def view(operation: MemoryMutationOperation) -> dict[str, Any]:
        return {"operation_id": operation.operation_id, **operation.result}

    monkeypatch.setattr(svc, "_view", view)
    result = await svc.change_physical_state("bank", 1, operation_id="legacy-public", forget=True)
    baseline = next(row for row in created if isinstance(row, MemorySourceState))
    operations = [row for row in created if isinstance(row, MemoryMutationOperation)]
    outbox = next(row for row in created if isinstance(row, MemoryPublicationOutbox))
    assert baseline.source_revision == 1 and baseline.generation == 2 and baseline.state == "forgotten"
    assert len(operations) == 1 and operations[0].operation_id == outbox.operation_id == result["operation_id"] == "legacy-public"
    assert outbox.payload["message_ids"] == [1] and result["stop_supply"] == "withdrawn"
    assert db.commits == 1


async def test_public_withdrawal_still_requires_explicit_adoption(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Session()
    svc = service(db)
    monkeypatch.setattr(svc, "_replay", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_bank", AsyncMock(return_value=SimpleNamespace(generation=1)))
    monkeypatch.setattr(svc, "_operation", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_source", AsyncMock(return_value=None))
    with pytest.raises(RevisionConflict, match="source_adoption_required"):
        await svc.withdraw_source("bank", 1, operation_id="withdraw-public", expected_source_revision=1, reason="explicit withdrawal")
    assert db.commits == 0 and db.rollbacks == 1


async def test_finish_attempt_never_clears_a_newer_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    row = SimpleNamespace(receipt={"active_attempts": ["old", "new"], "reservation_released": True}, lease_token="new", lease_until="later")
    db = Session()
    db.scalar = AsyncMock(return_value=row)  # type: ignore[attr-defined]
    svc = service(db)
    await svc._finish_attempt("bank", "operation", "old")
    assert row.receipt == {"active_attempts": ["new"], "finished_attempts": ["old"], "reservation_released": True}
    assert row.lease_token == "new" and row.lease_until == "later"
