"""Owned RAW sources and controlled summaries for real artifact API/storage tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.memory_compaction_models import MemoryCompactionArtifact, MemoryCompactionRoot
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_writes_http import WriteChat, api_request, result
from tests.support.memory_writes_http import writes_api as writes_api


@dataclass
class RetirementCommitFault:
    injected: bool = False
    commit_completed: bool = False


@contextmanager
def retirement_commit_fault(
    env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    artifact_id: str,
    operation_id: str,
    phase: Literal["before_commit", "after_commit"],
) -> Iterator[RetirementCommitFault]:
    """Fail the commit boundary of one real, flushed retirement on the owned database."""
    database = env["engine"].url.database
    assert isinstance(database, str) and database.startswith("multirag_test_")
    assert env["engine"].dialect.name == "postgresql"
    marker = "owned_retirement_commit_fault"
    original_commit = AsyncSession.commit
    fault = RetirementCommitFault()

    def mark_retirement(session: Session, flush_context: Any, instances: Any) -> None:
        if any(
            isinstance(item, MemoryCompactionArtifact)
            and item.memory_id == env["memory_id"]
            and item.artifact_id == artifact_id
            and item.operation_id == operation_id
            and item.retired_at is not None
            and sa.inspect(item).attrs.retired_at.history.has_changes()
            for item in session.dirty
        ):
            binding = session.get_bind()
            engine = binding if isinstance(binding, sa.engine.Engine) else binding.engine
            assert engine.url.database == database
            session.info[marker] = True

    async def interrupt_commit(session: AsyncSession) -> None:
        if not session.sync_session.info.pop(marker, False):
            await original_commit(session)
            return
        assert not fault.injected
        flushed = (
            await session.execute(
                sa.select(MemoryCompactionArtifact.retired_at, MemoryCompactionArtifact.package.is_(None)).where(
                    MemoryCompactionArtifact.artifact_id == artifact_id,
                    MemoryCompactionArtifact.memory_id == env["memory_id"],
                    MemoryCompactionArtifact.operation_id == operation_id,
                )
            )
        ).one()
        assert flushed[0] is not None and flushed[1] is True
        assert not await session.scalar(sa.select(sa.exists().where(MemoryCompactionRoot.artifact_id == artifact_id)))
        if phase == "after_commit":
            await original_commit(session)
            fault.commit_completed = True
        fault.injected = True
        env["journal"].event("retirement-commit-fault-injected", database=database, artifact_id=artifact_id, operation_id=operation_id, phase=phase, commit_completed=fault.commit_completed)
        raise sa.exc.OperationalError("controlled retirement commit acknowledgement failure", {}, RuntimeError("controlled test fault"))

    env["journal"].event("reserve-retirement-commit-fault", database=database, artifact_id=artifact_id, operation_id=operation_id, phase=phase)
    sa.event.listen(Session, "before_flush", mark_retirement)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(AsyncSession, "commit", interrupt_commit)
            yield fault
        assert fault.injected and fault.commit_completed is (phase == "after_commit")
    finally:
        sa.event.remove(Session, "before_flush", mark_retirement)


@pytest.fixture
def compaction_api(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = writes_api
    env["summary_calls"] = []
    env["summary_response"] = "仅内部合成评审适用：先核对来源与条件，保留禁止生产用途的约束。"

    async def summarize(model: WriteChat, system: str, history: list[dict[str, Any]], generation: dict[str, Any]) -> tuple[str, int]:
        assert "unverified context data" in system and generation["max_tokens"] > 0
        env["summary_calls"].append({"system": system, "history": history, "generation": generation})
        model.calls.append(history[-1]["content"])
        if "summary_started" in env:
            import asyncio

            env["summary_started"].set()
            assert await asyncio.to_thread(env["summary_release"].wait, 30)
        if "summary_exception" in env:
            raise env["summary_exception"]
        return env["summary_response"], 0

    monkeypatch.setattr(WriteChat, "async_chat", summarize)
    identities = []
    try:
        for index in range(2):
            value = result(
                api_request(
                    env,
                    "POST",
                    f"/memories/{env['memory_id']}/sources",
                    json={
                        "operation_id": f"compaction-source-{index}",
                        "agent_id": "compaction-agent",
                        "session_id": f"source-session-{index}",
                        "segments": [
                            {"role": "user", "content": ("仅内部合成评审，先核对来源与条件，禁止生产用途。" * (120 if index == 0 else 3))},
                            {"role": "assistant", "content": "这是助手声明，不是工具执行回执。"},
                        ],
                        "event_time": None,
                    },
                )
            )
            identity = value["raw_source_id"]
            identities.append(identity)
            state = result(api_request(env, "GET", f"/memories/{env['memory_id']}/sources/{identity}/state"))
            result(
                api_request(
                    env, "POST", f"/memories/{env['memory_id']}/sources/{identity}/adoptions", json={"operation_id": f"compaction-register-{index}", "expected_content_sha256": state["content_sha256"]}
                )
            )
        env["compaction_source_ids"] = identities
        yield env
    finally:
        with Session(env["engine"]) as writer:
            writer.execute(sa.delete(MemoryCompactionRoot).where(MemoryCompactionRoot.memory_id.in_(list(env["memories"].values()))))
            writer.execute(sa.delete(MemoryCompactionArtifact).where(MemoryCompactionArtifact.memory_id.in_(list(env["memories"].values()))))
            writer.commit()
        with Session(env["engine"]) as reader:
            counts = {
                model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(model.memory_id.in_(list(env["memories"].values()))))
                for model in (MemoryCompactionRoot, MemoryCompactionArtifact)
            }
            assert not any(counts.values()), counts
        env["journal"].event("compaction-sql-cleanup-readback", remaining=counts)


def request(env: dict[str, Any], operation_id: str = "compact-owned") -> dict[str, Any]:
    return {"operation_id": operation_id, "source_ids": list(env["compaction_source_ids"]), "prefix_count": 1, "input_budget_tokens": 16384, "output_budget_tokens": 1024, "budget_tokens": 4096}
