"""Bounded Milvus inventory, SQL transaction/RPC observations and pinned consumer."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Iterator
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal, cast

import pytest
import sqlalchemy as sa
from pymilvus import Collection, utility
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from api.db.memory_revision_models import MemorySourceState
from common import settings
from memory.services.startup import StartupService
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_startup_milvus_scale import MESSAGE_BASE, SCOPE, SOURCE_ID, cleanup_verifications, fingerprint, observation_record, observe_reads, preview, seed_inventory
from tests.support.memory_writes_http import result
from tests.support.memory_writes_http import writes_api as writes_api
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file


@pytest.fixture
def scale_api(writes_api: dict[str, Any], request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    env = writes_api
    assert "Milvus" in type(settings.msgStoreConn).__name__, "This test requires real Milvus, never a fallback backend"
    env["startup_runtime_versions"] = {"pymilvus": version("pymilvus"), "milvus_server": utility.get_server_version(using=settings.msgStoreConn._using)}
    env["journal"].event("startup-milvus-runtime-preflight", **env["startup_runtime_versions"])
    size, confirmed = getattr(request, "param", (130, 128))
    try:
        seed_inventory(env, size=size, confirmed=confirmed)
        yield env
    finally:
        cleanup_verifications(env)


def test_actual_milvus_32_64_128_inventory_budget_and_rpc_limits(scale_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = scale_api
    before = fingerprint(env)
    observations = []
    calls = len(env["embedding_calls"]), len(env["chat_calls"])
    for limit in (32, 64, 128):
        with monkeypatch.context() as patch, observe_reads(env, patch) as observed:
            response = preview(env, inventory_limit=limit)
        assert response.headers.get("cache-control") == "no-store"
        packet = result(response)
        record = observation_record(env, observed, packet, label=f"inventory-{limit}")
        assert packet["selection"]["scanned_count"] == limit and packet["selection"]["inventory_truncated"]
        assert packet["selection"]["physical_reads"] == min(limit * 4, 256)
        assert [item["entry_id"] for item in packet["items"]] == env["scale_entry_ids"][:16]
        assert all(item["verification"]["result"] == "human_confirmed" and item["assessment"]["verification_status"] == "unverified" for item in packet["items"])
        assert all(len(item["sources"]) == 1 and item["sources"][0]["source_id"] == str(SOURCE_ID) for item in packet["items"])
        assert not packet["selection"]["selection_complete"]
        observations.append(record)
    assert packet["selection"]["read_limited"] and packet["omitted"]["counts"]["physical_read_limit"] == 64
    # SQL metadata work must not grow linearly with every physical single read.
    assert observations[-1]["sql_statement_count"] <= observations[0]["sql_statement_count"] + 40
    assert observations[-1]["sql_statement_count"] <= 100
    for label, body in (("whole-budget", {"budget_tokens": 1024}), ("max-items", {"max_items": 1})):
        with monkeypatch.context() as patch, observe_reads(env, patch) as observed:
            packet = result(preview(env, inventory_limit=32, **body))
        observation_record(env, observed, packet, label=label)
        assert packet["selection"]["scanned_count"] == 32 and packet["selection"]["physical_reads"] == 128
        if label == "whole-budget":
            assert packet["omitted"]["counts"].get("budget", 0) > 0
        else:
            assert len(packet["items"]) == 1 and packet["omitted"]["counts"]["max_items"] == 31
    assert (len(env["embedding_calls"]), len(env["chat_calls"])) == calls
    assert fingerprint(env) == before
    env["journal"].event("startup-milvus-scale-no-model-readback", model_call_counts_unchanged=True, observations=observations)


@pytest.mark.parametrize("scale_api", [(4, 4)], indirect=True)
def test_final_fresh_metadata_rejects_source_generation_writer(scale_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, original = scale_api, StartupService._metadata_phase
    fired = False
    initial = result(preview(env, entry_ids=[env["scale_entry_ids"][0]]))
    assert initial["items"]

    async def phase(self: StartupService, memory_id: str, entry_ids: list[str], *, stage: Literal["before", "after_first", "after_second", "closing"] = "before") -> Any:
        nonlocal fired
        if memory_id == env["memory_id"] and stage == "closing" and not fired:
            fired = True
            assert not self.db.in_transaction()
            env["journal"].event("reserve-startup-final-source-generation-race", memory_id=memory_id, source_id=SOURCE_ID)

            def change() -> None:
                with Session(env["engine"]) as writer:
                    changed = writer.execute(
                        sa.update(MemorySourceState)
                        .where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == SOURCE_ID)
                        .values(generation=MemorySourceState.generation + 1)
                    )
                    assert cast(CursorResult[Any], changed).rowcount == 1
                    writer.commit()
                with Session(env["engine"]) as reader:
                    row = reader.get(MemorySourceState, (memory_id, SOURCE_ID))
                    assert row is not None and row.generation == 2

            await asyncio.to_thread(change)
        return await original(self, memory_id, entry_ids, stage=stage)

    monkeypatch.setattr(StartupService, "_metadata_phase", phase)
    response = preview(env, entry_ids=[env["scale_entry_ids"][0]])
    assert fired and response.status_code == 409, response.text
    assert initial["items"][0]["content"] not in response.text
    after = result(preview(env, entry_ids=[env["scale_entry_ids"][0]]))
    assert after["items"] == [] and after["omitted"]["counts"].get("verification_stale", 0) == 1


@pytest.mark.parametrize("scale_api", [(2, 1)], indirect=True)
def test_actual_milvus_alias_projection_is_not_truncated_into_success(scale_api: dict[str, Any]) -> None:
    env = scale_api
    original = result(preview(env, entry_ids=[env["scale_entry_ids"][0]]))
    assert original["items"]
    message = {**env["scale_messages"][0], "id": f"{env['memory_id']}_alias_{MESSAGE_BASE}", "content": "Alias content must never be supplied as the canonical memory."}
    env["journal"].event("reserve-startup-alias-projection", memory_id=env["memory_id"], message_id=MESSAGE_BASE, alias_primary_key=message["id"])
    storage = settings.msgStoreConn
    # Author one old/corrupt PK directly; production writers must keep refusing aliases.
    payload = storage.map_message_to_milvus_fields(env["scale_messages"][0])
    payload.update(id=message["id"], content_ltks=message["content"])
    collection = Collection(storage_index(env) + "_" + env["memory_id"], using=storage._using)
    ack = collection.upsert([payload], retry_times=0, retry_on_rate_limit=False)
    assert ack.upsert_count == 1
    rows = storage.query_source_rows(["id", "message_id", "content", "status", "memory_id"], [MESSAGE_BASE], storage_index(env), env["memory_id"])
    assert len(rows) == 2, "The real scalar-ID query must expose the extra alias sentinel"
    response = preview(env, entry_ids=[env["scale_entry_ids"][0]])
    assert response.status_code == 500, response.text
    assert response.json()["data"] is False
    assert original["items"][0]["content"] not in response.text and message["content"] not in response.text
    env["journal"].event("startup-alias-fail-closed", status=response.status_code, scalar_matches=len(rows), no_body_supplied=True)


def storage_index(env: dict[str, Any]) -> str:
    from memory.services.messages import index_name

    return index_name(env["tenants"]["personal"])


@pytest.mark.external_consumer
@pytest.mark.parametrize("scale_api", [(4, 4)], indirect=True)
def test_pinned_web_startup_consumer_over_real_milvus(scale_api: dict[str, Any], tmp_path: Path) -> None:
    env = scale_api
    checkout, sha = pinned_web_checkout()
    journal = ResourceJournal("startup-milvus-scale-web")
    configuration, output = tmp_path / "startup-scale.private.json", tmp_path / "startup-scale-result.json"
    owned: list[Path] = []
    calls = len(env["embedding_calls"]), len(env["chat_calls"])
    before = fingerprint(env)
    try:
        private_consumer_file(
            configuration,
            json.dumps(
                {
                    "baseURL": env["base"],
                    "token": env["jwts"][1],
                    "memoryId": env["memory_id"],
                    "entryId": env["scale_entry_ids"][0],
                    "entryIds": env["scale_entry_ids"],
                    "userId": SCOPE["user_id"],
                    "agentId": SCOPE["agent_id"],
                    "ownedScratchConfirmed": True,
                    "expectedIncluded": env["scale_entry_ids"],
                    "phaseLabel": "batched-sql-real-milvus",
                    "resultFile": str(output),
                }
            ),
            journal,
            owned,
        )
        journal.event("reserve-private-consumer-file", path=str(output), creation_owner="exclusive pinned Web consumer")
        assert not output.exists()
        owned.append(output)
        completed = subprocess.run(
            ["node", "--import", "tsx", "scripts/consumers/memory-startup.ts", str(configuration)],
            cwd=checkout,
            env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"},
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        receipt = json.loads(output.read_text())
        assert receipt["passed"] and len(receipt["included"]) == 4 and receipt["readOnly"]
        assert (len(env["embedding_calls"]), len(env["chat_calls"])) == calls
        assert fingerprint(env) == before
        journal.event("startup-milvus-pinned-consumer-readback", web_sha=sha, receipt=receipt, model_calls_unchanged=True)
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("startup-milvus-consumer-files-cleaned", paths=[str(path) for path in owned], absent=True)
