"""Deterministic revisions, publication fences and transaction ownership contracts."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Memory
from api.db.memory_revision_models import MEMORY_REVISION_MODELS, ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from memory.revisions.domain import (
    MAX_CONTENT_BYTES,
    ModelBinding,
    RevisionBusy,
    RevisionConflict,
    RevisionPersistenceUnknown,
    RevisionValidationError,
    content_hash,
    positive_int,
    request_hash,
    revised_content,
    validate_adoption,
    validate_operation_id,
    validate_projection,
)
from memory.revisions.service import RevisionService


def message(message_id: int = 2, **changes: Any) -> dict[str, Any]:
    return {
        "memory_id": "bank",
        "message_id": message_id,
        "message_type": "semantic",
        "source_id": 1,
        "user_id": "",
        "agent_id": "agent",
        "session_id": "session",
        "content": "fact one; fact two",
        "content_embed": [1.0, 0.0],
        "status": True,
        "forget_at": None,
        **changes,
    }


def test_patch_preserves_sibling_facts_and_literal_whitespace() -> None:
    result, count = revised_content("fact one; fact two", content=None, patch={"old_text": "fact one", "new_text": " new fact "}, count_tokens=len)

    assert result == " new fact ; fact two" and count == len(result)


@pytest.mark.parametrize("body,old,code", [("aaa", "aa", "patch_ambiguous"), ("same same", "same", "patch_ambiguous"), ("e\u0301", "é", "patch_not_found"), ("keep", "missing", "patch_not_found")])
def test_patch_refuses_overlapping_ambiguous_and_normalized_guesses(body: str, old: str, code: str) -> None:
    with pytest.raises(RevisionConflict, match=code):
        revised_content(body, content=None, patch={"old_text": old, "new_text": "new"}, count_tokens=len)


@pytest.mark.parametrize(
    "content,patch",
    [(None, None), ("new", {"old_text": "old", "new_text": "new"}), (None, {"old_text": "", "new_text": "new"}), (None, {"old_text": "old", "new_text": "new", "tenant_id": "other"}), ("  ", None)],
)
def test_replacement_input_is_exclusive_strict_and_nonempty(content: str | None, patch: dict[str, Any] | None) -> None:
    with pytest.raises(RevisionValidationError):
        revised_content("old", content=content, patch=patch, count_tokens=len)


def test_utf8_budget_is_checked_before_token_counter() -> None:
    counter = MagicMock(side_effect=AssertionError("Over-byte content must not reach tokenizer"))
    with pytest.raises(RevisionValidationError, match="content_bytes_exceeded"):
        revised_content("old", content="中" * (MAX_CONTENT_BYTES // 3 + 1), patch=None, count_tokens=counter)
    counter.assert_not_called()


@pytest.mark.parametrize("estimate", [True, -1, 8193])
def test_invalid_or_excessive_token_estimate_is_not_accepted(estimate: Any) -> None:
    with pytest.raises(RevisionValidationError):
        revised_content("old", content="new", patch=None, count_tokens=lambda text: estimate)


def test_tokenizer_failure_does_not_become_zero_budget() -> None:
    with pytest.raises(RuntimeError, match="encoder failed"):
        revised_content("old", content="new", patch=None, count_tokens=MagicMock(side_effect=RuntimeError("encoder failed")))


@pytest.mark.parametrize("patch", [None, {"old_text": "old", "new_text": "old"}])
def test_unchanged_content_does_not_create_a_new_publication(patch: dict[str, str] | None) -> None:
    with pytest.raises(RevisionValidationError, match="content_unchanged"):
        revised_content("old", content="old" if patch is None else None, patch=patch, count_tokens=len)


@pytest.mark.parametrize("value", [True, False, 0, -1, "1", 1.0, 2**63])
def test_revision_and_message_ids_are_strict_signed_positive_integers(value: Any) -> None:
    with pytest.raises(RevisionValidationError):
        positive_int(value)


@pytest.mark.parametrize("value", ["", " has-space", "a/b", "a" * 129])
def test_operation_identifier_is_bounded_and_literal(value: str) -> None:
    with pytest.raises(RevisionValidationError):
        validate_operation_id(value)


def test_idempotency_digest_binds_actor_scope_and_exact_request() -> None:
    request = {"actor": "one", "tenant": "tenant", "memory": "bank", "content": "new"}
    assert request_hash(request) == request_hash(dict(reversed(list(request.items()))))
    for field in request:
        assert request_hash({**request, field: "different"}) != request_hash(request)


@pytest.mark.parametrize("change", [{"tenant_model_id": True}, {"dimension": True}, {"dimension": 0}, {"dimension": 32769}, {"model_reference": ""}])
def test_model_binding_rejects_invalid_identity_and_dimension(change: dict[str, Any]) -> None:
    with pytest.raises(RevisionValidationError):
        ModelBinding(**{"tenant_model_id": "00000000000000000000000000000001", "model_reference": "embed", "dimension": 2, **change})


def test_adoption_keeps_real_raw_scope_and_baseline_dimension() -> None:
    source = message(1, message_type="raw", source_id=0)
    scope, dimension = validate_adoption("bank", 2, message(), source)
    assert scope == {"user_id": "", "agent_id": "agent", "session_id": "session"} and dimension == 2


@pytest.mark.parametrize("change", [{"memory_id": "other"}, {"user_id": "other"}, {"message_type": "semantic"}, {"source_id": 9}, {"status": False}, {"forget_at": "2026-11-15"}])
def test_adoption_refuses_unavailable_or_unrelated_parent(change: dict[str, Any]) -> None:
    with pytest.raises(RevisionConflict):
        validate_adoption("bank", 2, message(), {**message(1, message_type="raw", source_id=0), **change})


def test_projection_receipt_proves_binding_but_contains_no_body_or_vector() -> None:
    staged = message(3, status=False)
    receipt = validate_projection(staged, staged, ModelBinding("00000000000000000000000000000001", "embed", 2), active=False)
    assert receipt["content_sha256"] == content_hash(staged["content"])
    assert "content" not in receipt and "content_embed" not in receipt


@pytest.mark.parametrize(
    "change",
    [
        {"content": "foreign body"},
        {"message_id": 4},
        {"id": "foreign-physical-id"},
        {"source_id": 9},
        {"valid_at": "changed time"},
        {"content_embed": [1.0]},
        {"content_embed": [float("nan"), 0.0]},
        {"user_id": "other"},
        {"status": True},
    ],
)
def test_projection_readback_mismatch_never_verifies(change: dict[str, Any]) -> None:
    staged = message(3, status=False)
    with pytest.raises(RevisionConflict):
        validate_projection({**staged, **change}, staged, ModelBinding("00000000000000000000000000000001", "embed", 2), active=False)


@pytest.fixture
def service() -> RevisionService:
    db = MagicMock(spec=AsyncSession)
    db.new, db.dirty, db.deleted = set(), set(), set()
    db.in_transaction.return_value = False
    db.begin, db.flush, db.commit, db.rollback, db.refresh, db.scalar, db.scalars = (AsyncMock() for _ in range(7))
    return RevisionService(
        db,
        tenant_id="tenant",
        actor_id="actor",
        message_reader=AsyncMock(),
        allocate_message_id=AsyncMock(),
        count_tokens=len,
        authorize=AsyncMock(),
        model_binding=AsyncMock(),
        measure_memory_bytes=AsyncMock(),
    )


async def test_commit_failure_is_unknown_with_same_operation_id(service: RevisionService) -> None:
    service.db.commit.side_effect = DBAPIError("COMMIT", {}, ConnectionError("lost acknowledgement"), connection_invalidated=True)
    with pytest.raises(RevisionPersistenceUnknown) as caught:
        async with service._write("same-operation"):
            pass
    assert caught.value.operation_id == "same-operation"
    service.db.commit.assert_awaited_once()
    service.db.rollback.assert_awaited_once()


async def test_constraint_failure_rolls_back_without_claiming_commit_unknown(service: RevisionService) -> None:
    service.db.flush.side_effect = IntegrityError("INSERT", {}, RuntimeError("unique collision"))
    with pytest.raises(RevisionConflict):
        async with service._write("same-operation"):
            pass
    service.db.rollback.assert_awaited_once()
    service.db.commit.assert_not_awaited()


async def test_repeated_cancel_drains_commit_before_propagation(service: RevisionService) -> None:
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def commit() -> None:
        started.set()
        await release.wait()
        finished.set()

    async def mutate() -> None:
        async with service._write("same-operation"):
            pass

    service.db.commit.side_effect = commit
    task = asyncio.create_task(mutate())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    service.db.commit.assert_awaited_once()


async def test_unadopted_entry_get_never_registers_or_reads_external_messages(service: RevisionService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_memory", AsyncMock())
    service.db.scalar.return_value = None
    assert await service.find_entry_by_message("bank", 2) is None
    service.db.add.assert_not_called()
    service.db.commit.assert_not_awaited()
    service.message_reader.assert_not_awaited()


async def test_dry_run_never_allocates_embeds_or_persists(service: RevisionService, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = {**message(), "model_binding": ModelBinding("00000000000000000000000000000001", "embed", 2).to_dict(), "embedding_dimension": 2}
    snapshot.pop("content_embed")
    entry = MemoryEntry(entry_id="entry", memory_id="bank", tenant_id="tenant", current_revision=1, current_message_id=2, state="active")
    revision = MemoryEntryRevision(entry_id="entry", revision=1, content="fact one; fact two", message_snapshot=snapshot)
    ref = MemoryRevisionSource(source_message_id=1, source_revision=1, source_generation=1, content_sha256="hash", relation="context", locator={})

    async def sources(*args: Any, **kwargs: Any) -> list[MemoryRevisionSource]:
        kwargs["captured_generations"][1] = 3
        return [ref]

    monkeypatch.setattr(service, "_memory", AsyncMock())
    monkeypatch.setattr(service, "_entry", AsyncMock(return_value=entry))
    monkeypatch.setattr(service, "_revision", AsyncMock(return_value=revision))
    monkeypatch.setattr(service, "_sources", sources)
    result = await service.revise_entry("bank", "entry", operation_id="preview", expected_revision=1, patch={"old_text": "fact one", "new_text": "new fact"}, reason="correction", dry_run=True)
    assert result["content"] == "new fact; fact two" and result["source_checks"][0]["generation"] == 3
    assert result["diff"]["old_text"] == "fact one; fact two"
    service.allocate_message_id.assert_not_awaited()
    service.measure_memory_bytes.assert_not_awaited()
    service.model_binding.assert_not_awaited()
    service.db.add.assert_not_called()
    service.db.commit.assert_not_awaited()


async def test_new_revision_captures_reenabled_source_generation_without_rewriting_history(service: RevisionService, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = message()
    ref = MemoryRevisionSource(entry_id="entry", revision=1, memory_id="bank", source_message_id=1, source_revision=1, source_generation=1, content_sha256="hash", locator={})
    source = MemorySourceState(
        memory_id="bank",
        source_message_id=1,
        tenant_id="tenant",
        source_revision=1,
        generation=3,
        content_sha256="hash",
        state="active",
        scope={"user_id": "", "agent_id": "agent", "session_id": "session"},
    )
    service.db.scalars.return_value = SimpleNamespace(all=lambda: [ref])
    service.db.scalar.return_value = source
    monkeypatch.setattr(service, "_revision", AsyncMock(return_value=MemoryEntryRevision(memory_id="bank", message_snapshot=snapshot)))
    captured: dict[int, int] = {}
    assert await service._sources("entry", 1, require_generation=False, captured_generations=captured) == [ref]
    assert captured == {1: 3} and ref.source_generation == 1
    with pytest.raises(RevisionConflict, match="source_state_changed"):
        await service._sources("entry", 1)


async def test_drained_old_attempt_does_not_clear_newer_lease_even_after_withdrawal(service: RevisionService, monkeypatch: pytest.MonkeyPatch) -> None:
    until = datetime.now(UTC) + timedelta(seconds=60)
    row = MemoryPublicationOutbox(id="outbox", operation_id="op", memory_id="bank", state="stale", lease_token="new", lease_until=until, receipt={"active_attempts": ["old", "new"]})
    monkeypatch.setattr(service, "_outbox", AsyncMock(return_value=row))
    service.db.scalar.return_value = ManagedMemoryState(memory_id="bank", tenant_id="tenant", state="deleted")
    await service.finish_publication_attempt("outbox", lease_token="old")
    assert row.state == "stale" and row.lease_token == "new" and row.lease_until == until
    assert row.receipt["active_attempts"] == ["new"] and row.receipt["finished_attempts"] == ["old"]
    await service.finish_publication_attempt("outbox", lease_token="old")
    assert row.receipt["finished_attempts"] == ["old"]


@pytest.mark.parametrize("changed", [False, True])
async def test_publication_uses_resolved_model_identity_for_legacy_alias(service: RevisionService, monkeypatch: pytest.MonkeyPatch, changed: bool) -> None:
    memory = Memory(id="bank", tenant_id="tenant", tenant_embd_id=None, embd_id="legacy-alias")
    binding = ModelBinding("00000000000000000000000000000047", "actual-model@Provider", 2, "resolved-config")
    row = MemoryPublicationOutbox(
        memory_id="bank",
        entry_id="entry",
        revision=2,
        message_id=3,
        expected_revision=1,
        bank_generation=1,
        entry_generation=1,
        state="staged",
        payload={"action": "replace", "model_binding": binding.to_dict()},
    )
    bank = ManagedMemoryState(generation=1, state="active")
    entry = MemoryEntry(current_revision=1, generation=1, state="active")
    monkeypatch.setattr(service, "_bank", AsyncMock(return_value=(memory, bank)))
    monkeypatch.setattr(service, "_sources", AsyncMock())
    monkeypatch.setattr(service, "_entry", AsyncMock(return_value=entry))
    service.model_binding.return_value = ModelBinding("00000000000000000000000000000048", "changed-model@Provider", 2, "new-config") if changed else binding
    if changed:
        with pytest.raises(RevisionConflict, match="model_binding_changed"):
            await service._publication_fence(row)
    else:
        assert await service._publication_fence(row) == (bank, entry)


@pytest.mark.parametrize("token,expiry", [(None, 60), ("other", 60), ("owner", -1)])
def test_publication_lease_is_fenced(token: str | None, expiry: int) -> None:
    row = MemoryPublicationOutbox(lease_token="owner", lease_until=datetime.now(UTC) + timedelta(seconds=expiry))
    with pytest.raises(RevisionBusy):
        RevisionService._lease(row, token)


def test_managed_state_has_no_foreign_key_to_remote_messages_or_deletable_memory() -> None:
    names = {model.__tablename__ for model in MEMORY_REVISION_MODELS}
    for model in MEMORY_REVISION_MODELS:
        for constraint in model.__table__.foreign_keys:
            assert constraint.column.table.name in names and constraint.ondelete == "RESTRICT"
