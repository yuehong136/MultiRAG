"""Shared owned incremental-memory HTTP fixtures and bounded command helpers."""

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.memory_incremental_models import MemoryMaintenanceBatch, MemoryMaintenanceCoverage, MemoryMaintenanceDiscovery, MemoryMaintenanceFragmentCoverage
from memory.revisions.domain import content_hash
from memory.revisions.storage import ManagedMessageStore
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_dreaming_http import dreaming_api as dreaming_api
from tests.support.memory_writes_http import WriteChat, api_request, result
from tests.support.memory_writes_http import writes_api as writes_api

LARGE_ID = 9007199254740993
SCOPE = {"user_id": "existing-subject", "agent_id": "existing-agent", "session_id": None}


def seed_source(env: dict[str, Any], identity: int, text: str, *, subject: str = "existing-subject", session: str = "later-session") -> None:
    mid = env["memory_id"]
    env["journal"].event("reserve-incremental-raw-source", memory_id=mid, message_id=identity)
    store = ManagedMessageStore(env["tenants"]["personal"], mid)
    asyncio.run(
        store.write(
            {
                "id": f"{mid}_{identity}",
                "memory_id": mid,
                "message_id": identity,
                "message_type": "raw",
                "source_id": 0,
                "user_id": subject,
                "agent_id": "existing-agent",
                "session_id": session,
                "content": text,
                "content_embed": [1.0, 0.0],
                "valid_at": "2020-01-01 00:00:00",
                "invalid_at": None,
                "forget_at": None,
                "status": False,
            }
        )
    )
    asyncio.run(store.set_status(identity, True))
    result(
        api_request(
            env,
            "POST",
            f"/memories/{mid}/sources/{identity}/adoptions",
            json={"operation_id": f"register-incremental-{identity}", "expected_content_sha256": content_hash(text)},
        )
    )
    env["source_texts"][str(identity)] = text


@pytest.fixture
def incremental_api(dreaming_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    env = dreaming_api
    options = getattr(request, "param", {})
    env["source_texts"] = {"900": "Owned raw personal"}
    env["incremental_ids"] = ["900", "901", "902", str(LARGE_ID)]
    for identity in (901, 902, LARGE_ID):
        content = "An enduring source-specific constraint " + str(identity) + "."
        if identity == options.get("oversize"):
            content = "A complete oversized source: " + "measurement premise; " * 1800
        seed_source(env, identity, content)
    seed_source(env, 904, "A different subject's constraint.", subject="other-subject")

    async def chat(self: WriteChat, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> tuple[str, int]:
        assert "never publish" in system and gen_conf["max_tokens"] <= 8192
        payload = json.loads(history[-1]["content"])
        env["analysis_calls"].append(payload)
        if "analysis_started" in env:
            env["analysis_started"].set()
            await asyncio.to_thread(env["analysis_release"].wait, 30)
        if env.get("invalid_response"):
            return json.dumps(
                {
                    "proposals": [
                        {
                            "action": "patch",
                            "target_entry_id": "f" * 32,
                            "patch": {"old_text": "missing", "new_text": "invented"},
                            "source_input_ids": [payload["inputs"][0]["input_id"]],
                            "reason": "invalid target",
                        }
                    ]
                }
            ), 0
        proposals = [
            {
                "action": "add",
                "message_type": "semantic",
                "content": "Source reports: " + item["snapshot"]["content"],
                "source_input_ids": [item["input_id"]],
                "reason": "Unverified controlled proposal for explicit human review.",
            }
            for item in payload["inputs"]
            if str(item["source_id"]) not in env.get("empty_sources", set())
        ]
        return json.dumps({"proposals": proposals}), 0

    monkeypatch.setattr(WriteChat, "async_chat", chat)
    try:
        yield env
    finally:
        if "analysis_release" in env:
            env["analysis_release"].set()
        env["worker_stop"].set()
        env["worker_thread"].join(timeout=40)
        assert not env["worker_thread"].is_alive(), "owned incremental worker did not drain"
        # Remove only this fixture's metadata before the parent fixture removes
        # its child jobs. The scratch database is independently dropped later.
        with Session(env["engine"]) as db:
            for model in (MemoryMaintenanceFragmentCoverage, MemoryMaintenanceCoverage, MemoryMaintenanceBatch, MemoryMaintenanceDiscovery):
                db.execute(sa.delete(model).where(model.memory_id == env["memory_id"]))
            db.commit()
        with Session(env["engine"]) as reader:
            remaining = {
                model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(model.memory_id == env["memory_id"]))
                for model in (MemoryMaintenanceFragmentCoverage, MemoryMaintenanceCoverage, MemoryMaintenanceBatch, MemoryMaintenanceDiscovery)
            }
            assert not any(remaining.values()), remaining
        env["journal"].event("incremental-metadata-cleanup-readback", memory_id=env["memory_id"], remaining=remaining)


def selection(**overrides: Any) -> dict[str, Any]:
    return {"scope": dict(SCOPE), "policy": "general", "entry_ids": [], "batch_size": 2, "max_sources": 128, "input_budget_tokens": 16384, "output_budget_tokens": 4096, **overrides}


def endpoint(env: dict[str, Any], suffix: str = "") -> str:
    return f"/memories/{env['memory_id']}/maintenance-discoveries" + suffix


def preview(env: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    return result(api_request(env, "POST", endpoint(env, "/preview"), json=request))


def create(env: dict[str, Any], request: dict[str, Any], *, operation: str = "create-incremental") -> dict[str, Any]:
    observed = preview(env, request)
    response = api_request(env, "POST", endpoint(env), json={**request, "operation_id": operation, "expected_preview_sha256": observed["preview_sha256"]})
    assert response.status_code in {200, 201, 202}, response.text
    return result(response, status=response.status_code)


def run(env: dict[str, Any], plan: dict[str, Any], *, operation: str = "run-incremental", limit: int = 8) -> dict[str, Any]:
    response = api_request(env, "POST", endpoint(env, f"/{plan['discovery_id']}/runs"), json={"operation_id": operation, "limit": limit})
    assert response.status_code in {200, 202}, response.text
    return result(response, status=response.status_code)


def wait_plan(env: dict[str, Any], identity: str, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        assert not env.get("worker_errors"), env.get("worker_errors")
        observed = result(api_request(env, "GET", endpoint(env, "/" + identity)))
        if predicate(observed):
            return observed
        time.sleep(0.05)
    raise AssertionError("incremental discovery did not reach its expected state")
