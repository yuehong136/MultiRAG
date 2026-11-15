"""A failed follow-up diagnostic never erases an unknown child commit handle."""

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from memory.maintenance.incremental import DiscoveryService
from memory.maintenance.jobs import JobService
from memory.revisions.domain import RevisionConflict, RevisionPersistenceUnknown


@pytest.mark.parametrize("mark_error", [RevisionConflict("bank_deleting"), OSError("private diagnostic must not escape")])
async def test_unknown_child_keeps_parent_operation_when_followup_mark_fails(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, mark_error: Exception) -> None:
    async with AsyncSession() as db:

        class Jobs:
            def __init__(self) -> None:
                self.db, self.tenant_id, self.actor_id = db, "tenant", "actor"

            async def authorize(self, session: AsyncSession, memory_id: str) -> None:
                return None

            async def create(self, memory_id: str, **options: Any) -> dict[str, Any]:
                assert options["operation_id"] == "fixed-child-create"
                raise RevisionPersistenceUnknown("fixed-child-create")

        async def identity(memory_id: str) -> dict[str, str]:
            raise AssertionError("No fresh analysis or model call is allowed")

        owner = DiscoveryService(db, jobs=cast(JobService, Jobs()), identity=identity)

        async def reserve(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "runnable": True,
                "request": {"policy": "general", "input_budget_tokens": 1024, "output_budget_tokens": 256},
                "command": {"submitted_batch_ids": []},
                "plans": [{"batch_id": "batch", "job_id": "job", "create_operation_id": "fixed-child-create", "analysis_operation_id": "fixed-child-analysis", "source_ids": [1], "entry_ids": []}],
            }

        async def mark(*args: Any, **kwargs: Any) -> None:
            raise mark_error

        monkeypatch.setattr(owner, "_reserve_command", reserve)
        monkeypatch.setattr(owner, "_mark", mark)
        with pytest.raises(RevisionPersistenceUnknown) as raised:
            await owner.run("bank", "discovery", operation_id="original-parent-run", limit=1)
        assert raised.value.operation_id == "original-parent-run"
        assert isinstance(raised.value.__cause__, RevisionPersistenceUnknown)
        assert "Unable to record unknown incremental child outcome" in caplog.text
        assert "private diagnostic" not in caplog.text
