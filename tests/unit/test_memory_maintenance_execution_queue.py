"""Execution DTOs and worker stopping do not infer actual IO completion."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.db.memory_execution_models import MemoryMaintenanceExecution
from memory.maintenance import execution, worker


def test_execution_view_hides_ownership_and_can_report_a_fresh_drained_attempt() -> None:
    row = MemoryMaintenanceExecution(
        job_id="job",
        memory_id="bank",
        tenant_id="tenant",
        actor_id="actor",
        operation_id="analyze",
        request_sha256="0" * 64,
        job_generation=1,
        state="needs_reconciliation",
        claim_token="secret-owner",
        worker_id="private-worker",
        writer_token="private-attempt",
        attempt=1,
        queued_at=datetime.now(UTC),
        receipt={"needs_operator_reconciliation": True},
    )
    public = execution.execution_view(row)
    assert public["needs_operator_reconciliation"] is True
    assert not {"claim_token", "worker_id", "writer_token"} & public.keys()
    assert execution.execution_view(row, actual_attempt_active=False)["needs_operator_reconciliation"] is False
    assert row.state == "needs_reconciliation"


async def test_terminal_sql_uncertainty_stops_the_worker_without_repeating_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    process = AsyncMock(side_effect=execution.ExecutionBookkeepingUnknown("stable-operation"))
    monkeypatch.setattr(execution, "process_next", process)
    with pytest.raises(execution.ExecutionBookkeepingUnknown) as failure:
        await worker.run_worker(AsyncMock(), AsyncMock(), worker_id="owned-worker", poll_interval=0.05)
    assert failure.value.operation_id == "stable-operation" and process.await_count == 1


async def test_once_worker_returns_after_one_queue_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    process = AsyncMock(return_value=False)
    monkeypatch.setattr(execution, "process_next", process)
    await worker.run_worker(AsyncMock(), AsyncMock(), worker_id="owned-worker", once=True)
    assert process.await_count == 1


def test_cli_help_and_invalid_options_do_not_initialize_the_application() -> None:
    with pytest.raises(SystemExit) as result:
        worker.main(["--help"])
    assert result.value.code == 0
    with pytest.raises(SystemExit) as result:
        worker.main(["--claim-timeout-seconds", "5", "--heartbeat-seconds", "10"])
    assert result.value.code == 2


async def test_shutdown_closes_remaining_resources_after_one_close_failure() -> None:
    events: list[str] = []

    def fail() -> None:
        events.append("failed-connector")
        raise RuntimeError("close failed")

    def close() -> None:
        events.append("other-connector")

    async def dispose() -> None:
        events.append("async-engine")

    with pytest.raises(RuntimeError, match="close failed"):
        await worker._close_connections([SimpleNamespace(close=close), SimpleNamespace(close=fail)], SimpleNamespace(dispose=dispose), SimpleNamespace(dispose=lambda: events.append("sync-engine")))
    assert events == ["failed-connector", "other-connector", "async-engine", "sync-engine"]
