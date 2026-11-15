"""Operator recovery drains an exact attested attempt without inventing readiness."""

from typing import Any

import pytest

from memory.revisions.domain import RevisionConflict
from memory.writes.admission import close_writer_admissions, writer_drain_snapshot
from memory.writes.operators import acknowledge_attempt_quiescence
from tests.support.memory_append_repository import _service
from tests.support.memory_append_repository import environment as environment


async def test_attempt_attestation_is_exact_replayable_and_keeps_publication_pending(environment: dict[str, Any]) -> None:
    env = environment
    async with env["factory"]() as db:
        service = _service(db, env)
        await service.admit("bank", operation_id="crashed", kind="extract", request={"source_id": 9}, source_id=9)
        claimed = await service.claim("bank", "crashed")
        token = claimed["active_token"]
        options = {"memory_id": "bank", "tenant_id": "tenant", "operation_id": "crashed", "expected_generation": 1, "expected_attempt": 1, "evidence_reference": "owned-process-stop-report"}
        with pytest.raises(RevisionConflict, match="operator_attestation_attempt_changed"):
            await acknowledge_attempt_quiescence(db, **options, expected_active_token="f" * 32)
        before = await service.snapshot("bank", "crashed")
        assert before["active_token"] == token
        result = await acknowledge_attempt_quiescence(db, **options, expected_active_token=token)
        assert result["verification"] == "operator_attestation" and result["publication_state"] == "pending"
        assert await acknowledge_attempt_quiescence(db, **options, expected_active_token=token) == result
        after = await service.snapshot("bank", "crashed")
        assert after["active_token"] is None and after["state"] == "drained"
        assert after["publication_state"] == "pending" and after["payload"]["approved"] is False
        assert (await service.operation("bank", "crashed"))["status"] == "accepted"
        await service.claim("bank", "crashed")
        with pytest.raises(RevisionConflict, match="operator_attestation_attempt_changed"):
            await acknowledge_attempt_quiescence(db, **options, expected_active_token=token)


async def test_closing_fence_rejects_stale_generation_before_draining_actual_attempt(environment: dict[str, Any]) -> None:
    env = environment
    async with env["factory"]() as db:
        service = _service(db, env)
        await service.admit("bank", operation_id="closing-crash", kind="extract", request={"source_id": 9}, source_id=9)
        claimed = await service.claim("bank", "closing-crash")
        await db.begin()
        await close_writer_admissions(db, memory_id="bank", tenant_id="tenant", operation_id="delete-bank")
        await db.commit()
        options = {
            "memory_id": "bank",
            "tenant_id": "tenant",
            "operation_id": "closing-crash",
            "expected_attempt": 1,
            "expected_active_token": claimed["active_token"],
            "evidence_reference": "owned-remote-request-drained",
        }
        with pytest.raises(RevisionConflict, match="operator_attestation_generation_changed"):
            await acknowledge_attempt_quiescence(db, **options, expected_generation=1)
        assert (await writer_drain_snapshot(db, memory_id="bank", tenant_id="tenant"))["drained"] is False
        await acknowledge_attempt_quiescence(db, **options, expected_generation=2)
        snapshot = await writer_drain_snapshot(db, memory_id="bank", tenant_id="tenant")
        assert snapshot["drained"] is True and snapshot["state"] == "closing"
