"""Representative bounded Milvus startup workload, never a provider benchmark."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

import pytest
from pymilvus import Collection, utility

from common import settings
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import MessageService, index_name
from memory.services.startup import StartupService
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_startup_milvus_scale import SCOPE, cleanup_verifications, fingerprint, observation_record, observe_reads, preview, seed_inventory
from tests.support.memory_writes_http import api_request, result
from tests.support.memory_writes_http import writes_api as writes_api
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file


@pytest.fixture
def workload_api(writes_api: dict[str, Any], request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    env = writes_api
    storage = settings.msgStoreConn
    assert "Milvus" in type(storage).__name__, "This workload requires real Milvus"
    tenant, mid = env["tenants"]["personal"], env["memory_id"]
    env["startup_runtime_versions"] = {"pymilvus": version("pymilvus"), "milvus_server": utility.get_server_version(using=storage._using)}
    env["journal"].event("reserve-startup-workload-dimension-rebuild", memory_id=mid, collection=index_name(tenant) + "_" + mid, dimension=1536, existing_owned_collection=True)
    storage.delete_idx(index_name(tenant), mid)
    assert not storage.index_exists_strict(index_name(tenant), mid)
    assert MessageService.create_index(tenant, mid, 1536)
    assert (
        MessageService.insert_message(
            [
                {
                    "message_id": 900,
                    "message_type": "raw",
                    "source_id": 0,
                    "memory_id": mid,
                    "user_id": "existing-subject",
                    "agent_id": "existing-agent",
                    "session_id": "existing-session",
                    "valid_at": "2026-11-15 08:00:00",
                    "invalid_at": None,
                    "forget_at": None,
                    "status": True,
                    "content": "Owned raw personal",
                    "content_embed": [1.0, *([0.0] * 1535)],
                }
            ],
            tenant,
            mid,
        )
        == []
    )
    size = getattr(request, "param", 32)
    try:
        seed_inventory(env, size=size, confirmed=size, vector_size=1536, source_count=3, long_text=True)
        env["journal"].event(
            "startup-workload-dataset",
            dimensions=1536,
            entry_count=size,
            shared_source_count=3,
            entry_content_bytes=[len(row["content"].encode()) for row in env["scale_messages"]],
            source_content_bytes=[len(row["content"].encode()) for row in env["scale_sources"]],
            content_sha256={str(row["message_id"]): hashlib.sha256(row["content"].encode()).hexdigest() for row in [*env["scale_sources"], *env["scale_messages"]]},
            runtime_versions=env["startup_runtime_versions"],
            paid_model_calls=False,
        )
        yield env
    finally:
        cleanup_verifications(env)


def test_1536_long_multisource_single_and_two_concurrent_reads(workload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = workload_api
    before, calls = fingerprint(env), (len(env["embedding_calls"]), len(env["chat_calls"]))
    assert all(2000 <= len(row["content"].encode()) <= 4000 for row in [*env["scale_sources"], *env["scale_messages"]])
    with monkeypatch.context() as patch, observe_reads(env, patch) as observed:
        packet = result(preview(env, inventory_limit=32, max_items=4))
    record = observation_record(env, observed, packet, label="1536-long-three-sources-single")
    assert len(packet["items"]) == 4 and packet["selection"]["physical_reads"] == 256
    assert packet["selection"]["scanned_count"] == 32
    assert all(len(item["sources"]) == 3 and item["content"] == env["scale_messages"][index]["content"] for index, item in enumerate(packet["items"]))
    env["journal"].event("reserve-startup-workload-thread-pool", max_workers=2, submissions=2, lifetime="joined before observation ends")
    with monkeypatch.context() as patch, observe_reads(env, patch, concurrent=True) as parallel, ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(preview, env, inventory_limit=32, max_items=4) for _ in range(2)]
        packets = [result(future.result(timeout=90)) for future in futures]
    assert packets == [packet, packet]
    assert parallel["physical_reads"] == 512
    counts = Counter(row["method"] for row in parallel["rpc"])
    assert counts["Query"] == 512 and all(row["consistency_level"] == 0 for row in parallel["rpc"] if row["method"] == "Query")
    env["journal"].event(
        "startup-workload-two-concurrent-observation",
        requests=2,
        rpc_counts=dict(counts),
        physical_read_attempts=parallel["physical_reads"],
        elapsed_seconds=parallel["elapsed_seconds"],
        sql_statement_count=len(parallel["sql"]),
        sql_statements=parallel["sql"],
        sql_transactions_at_io=parallel["sql_transaction_at_io"],
        own_session_transaction_asserted=True,
        note="Other concurrent request SQL transactions are allowed; each physical read asserts its own session idle.",
    )
    assert callable(getattr(ManagedMessageStore, "prepared_read_pass", None)), "The optimized-runtime acceptance requires its explicit read-pass hook"

    @asynccontextmanager
    async def unprepared(self: ManagedMessageStore) -> AsyncIterator[None]:
        yield

    with monkeypatch.context() as patch:
        patch.setattr(ManagedMessageStore, "prepared_read_pass", unprepared)
        with observe_reads(env, patch) as fallback:
            unchanged = result(preview(env, inventory_limit=32, max_items=4))
    assert unchanged == packet, "Preparation must preserve the complete packet on the same inventory"
    old_record = observation_record(env, fallback, unchanged, label="1536-long-three-sources-unprepared-comparison")
    assert old_record["rpc_counts"]["DescribeCollection"] > record["rpc_counts"]["DescribeCollection"]
    assert old_record["rpc_counts"]["Query"] == record["rpc_counts"]["Query"] == 256
    assert fingerprint(env) == before
    assert (len(env["embedding_calls"]), len(env["chat_calls"])) == calls
    env["journal"].event("startup-workload-no-write-readback", fingerprint_unchanged=True, model_calls_unchanged=True, single_observation=record)


@pytest.mark.external_consumer
@pytest.mark.parametrize("workload_api", [4], indirect=True)
def test_1536_long_multisource_pinned_web_consumer(workload_api: dict[str, Any], tmp_path: Path) -> None:
    env = workload_api
    checkout, sha = pinned_web_checkout()
    journal = ResourceJournal("startup-milvus-workload-web")
    configuration, output = tmp_path / "startup-workload.private.json", tmp_path / "startup-workload-result.json"
    owned: list[Path] = []
    calls, before = (len(env["embedding_calls"]), len(env["chat_calls"])), fingerprint(env)
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
                    "budgetTokens": 32768,
                    "maxItems": 4,
                    "ownedScratchConfirmed": True,
                    "expectedIncluded": env["scale_entry_ids"],
                    "phaseLabel": "1536-long-three-sources-real-milvus",
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
        assert (len(env["embedding_calls"]), len(env["chat_calls"])) == calls and fingerprint(env) == before
        journal.event("startup-milvus-workload-consumer-readback", web_sha=sha, receipt=receipt, model_calls_unchanged=True, fingerprint_unchanged=True)
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("startup-milvus-consumer-files-cleaned", paths=[str(path) for path in owned], absent=True)


@pytest.mark.parametrize("workload_api", [2], indirect=True)
@pytest.mark.parametrize("mutation", ["projection_rewrite", "collection_rebuild"])
def test_1536_fresh_second_pass_rejects_changed_physical_collection(workload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    env, original = workload_api, StartupService._metadata_phase
    first = result(preview(env, entry_ids=[env["scale_entry_ids"][0]]))
    assert len(first["items"]) == 1
    storage, fired = settings.msgStoreConn, False
    mid, tenant = env["memory_id"], env["tenants"]["personal"]
    replacement = {**env["scale_messages"][0], "content": "Changed physical body must not escape the current verification binding."}

    async def phase(self: StartupService, memory_id: str, entry_ids: list[str], *, stage: Literal["before", "after_first", "after_second", "closing"] = "before") -> Any:
        nonlocal fired
        if memory_id == mid and stage == "after_first" and not fired:
            fired = True
            assert not self.db.in_transaction()
            env["journal"].event(
                "reserve-startup-workload-physical-mutation", memory_id=mid, collection=index_name(tenant) + "_" + mid, mutation=mutation, message_id=replacement["message_id"], owned_only=True
            )

            def change() -> None:
                rows = [replacement]
                if mutation == "collection_rebuild":
                    storage.delete_idx(index_name(tenant), mid)
                    assert not storage.index_exists_strict(index_name(tenant), mid)
                    assert storage.create_idx(index_name(tenant), mid, 1536)
                    rows = [*env["scale_sources"], *env["scale_messages"][1:], replacement]
                # Model a legacy/out-of-band projection change in this owned collection only.
                collection = Collection(index_name(tenant) + "_" + mid, using=storage._using)
                ack = collection.upsert([storage.map_message_to_milvus_fields(row) for row in rows], retry_times=0, retry_on_rate_limit=False)
                assert ack.upsert_count == len(rows)

            await asyncio.to_thread(change)
        return await original(self, memory_id, entry_ids, stage=stage)

    with monkeypatch.context() as patch:
        patch.setattr(StartupService, "_metadata_phase", phase)
        response = preview(env, entry_ids=[env["scale_entry_ids"][0]])
    assert fired and response.status_code == 409, response.text
    assert first["items"][0]["content"] not in response.text and replacement["content"] not in response.text
    env["journal"].event("startup-workload-physical-mutation-fail-closed", mutation=mutation, status=response.status_code, no_body_supplied=True)
    if mutation == "projection_rewrite":
        env["journal"].event("reserve-startup-workload-projection-restore", memory_id=mid, message_id=env["scale_messages"][0]["message_id"])
        restored = Collection(index_name(tenant) + "_" + mid, using=storage._using).upsert([storage.map_message_to_milvus_fields(env["scale_messages"][0])], retry_times=0, retry_on_rate_limit=False)
        assert restored.upsert_count == 1
        assert result(preview(env, entry_ids=[env["scale_entry_ids"][0]]))["items"] == first["items"]
        source = first["items"][0]["sources"][0]
        withdrawn = api_request(
            env,
            "POST",
            f"/memories/{mid}/sources/{source['source_id']}/withdrawals",
            json={"operation_id": "withdraw-workload-source", "expected_source_revision": int(source["source_revision"]), "reason": "Synthetic workload source withdrawal, no business data."},
        )
        assert withdrawn.status_code == 200, withdrawn.text
        closed = result(withdrawn)
        assert closed["stop_supply"] == "withdrawn"
        packet = result(preview(env, entry_ids=[env["scale_entry_ids"][0]]))
        assert not packet["items"] and first["items"][0]["content"] not in json.dumps(packet, ensure_ascii=False)
        env["journal"].event("startup-workload-source-withdrawal-readback", source_id=source["source_id"], no_body_supplied=True, omission_counts=packet["omitted"]["counts"])
