"""Owned RAW sources and controlled summaries for real artifact API/storage tests."""

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.memory_compaction_models import MemoryCompactionArtifact, MemoryCompactionRoot
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_writes_http import WriteChat, api_request, result
from tests.support.memory_writes_http import writes_api as writes_api


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
