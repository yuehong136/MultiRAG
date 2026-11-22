"""Verify a killed compaction has no implicit retry or automatic slot release."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from api.db.memory_compaction_models import MemoryCompactionArtifact
from api.db.memory_revision_models import MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from common.config_utils import CONFIGS
from memory.revisions.storage import ManagedMessageStore
from memory.writes.operators import acknowledge_attempt_quiescence
from tests.manual.test_memory_same_point_seed import bootstrapped_engine as bootstrapped_engine
from tests.support.memory_compaction_http import compaction_api as compaction_api
from tests.support.memory_compaction_http import release_api as release_api
from tests.support.memory_compaction_http import request
from tests.support.memory_compaction_http import writes_api as writes_api
from tests.support.memory_writes_http import api_request, result
from tests.support.web_consumer import private_consumer_file


def _recover_in_web(env: dict[str, Any], context: dict[str, Any], work: Path) -> None:
    web = Path(os.environ["MULTIRAG_ROLLOUT_WEB"]).resolve(strict=True)
    assert web.is_relative_to(Path(os.environ["TMPDIR"]).resolve())
    assert os.environ["MULTIRAG_ROLLOUT_WEB_SHA"] == json.loads(Path(".github/consumer-web.json").read_text())["ref"]
    config = work / "crash-web.json"
    private_consumer_file(
        config,
        json.dumps(
            {
                "baseURL": env["base"],
                "token": context["token"],
                "memoryId": context["memory"],
                "operationId": context["body"]["operation_id"],
                "sourceIds": [str(source) for source in context["body"]["source_ids"]],
                "mode": "read",
                "expectedState": "canceled",
                "prefixCount": 1,
                "ownedScratchConfirmed": True,
            }
        ),
        env["journal"],
        [],
    )
    proc = subprocess.run(
        ["node", "--import", "tsx", "scripts/consumers/memory-compaction.ts", str(config)],
        cwd=web,
        env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"},
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["passed"] and receipt["readOnlyRecovery"]
    assert receipt["generationRequests"] == receipt["applicationRequests"] == 0
    env["journal"].event("hard-crash-fixed-web", web_sha=os.environ["MULTIRAG_ROLLOUT_WEB_SHA"], operation_id=context["body"]["operation_id"], read_only=True)


def test_compaction_hard_crash_recovery(compaction_api: dict[str, Any]) -> None:
    env = compaction_api
    mid, tenant = env["memory_id"], env["tenants"]["personal"]
    database = env["engine"].url
    journal = env["journal"]
    operation_id = "owned-compaction-hard-crash"
    base = f"/memories/{mid}"
    operation_path = base + "/context-compaction-operations/" + operation_id
    body = request(env, operation_id)
    publication = sa.select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == mid, MemoryPublicationOutbox.operation_id == operation_id)
    store = ManagedMessageStore(tenant, mid)
    raw_before = asyncio.run(store.inventory())
    work = Path(os.environ["TMPDIR"]).resolve() / "memory-post-checkpoint" / "crash"
    journal.event("reserve-compaction-process", path=str(work), database=database.database, operation_id=operation_id, memory_id=mid)
    work.mkdir(mode=0o700)
    context = {
        "database": database.database,
        "owned_postgres": [database.host, database.port, database.database],
        "owned_hosts": {service: CONFIGS[service]["hosts" if service == "milvus" else "host"] for service in ("milvus", "redis", "datav_redis", "minio")},
        "body": body,
        "memory": mid,
        "tenant": tenant,
        "token": env["jwts"][1],
    }
    command = [sys.executable, "-B", "-m", "tests.support.memory_compaction_process"]
    config = work / "generate.json"
    private_consumer_file(config, json.dumps({**context, "mode": "generate"}), journal, [])
    barrier = work / "durable-attempt.json"
    with subprocess.Popen([*command, str(config)]) as child:
        try:
            until = time.monotonic() + 90
            while not barrier.exists() and child.poll() is None and time.monotonic() < until:
                time.sleep(0.05)
            assert barrier.exists(), ("Child did not reach controlled provider", child.poll())
            marker = json.loads(barrier.read_text())
            assert marker == {"pid": child.pid, "model_calls": 1}
            with Session(env["engine"]) as db:
                writer = db.get(MemoryWriterAdmission, (mid, operation_id))
                outbox = db.scalars(publication).one()
                fence = db.get(MemoryWriterFence, mid)
                assert writer is not None and writer.active_token and writer.attempt == 1 and writer.state == "active"
                assert outbox is not None and outbox.receipt["active_attempts"] == [writer.active_token]
                assert fence is not None and fence.generation == writer.writer_generation
                attempt, token, generation = writer.attempt, writer.active_token, fence.generation
            child.kill()
            assert child.wait(timeout=15) == -signal.SIGKILL
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=15)
        journal.event("hard-crash-sigkill-observed", pid=child.pid, exit_code=child.returncode, operation_id=operation_id, attempt=attempt)

    restart = work / "restart.json"
    private_consumer_file(restart, json.dumps({**context, "mode": "restart"}), journal, [])
    subprocess.run([*command, str(restart)], check=True, timeout=90)
    busy = result(api_request(env, "GET", operation_path))
    assert busy["artifact"]["package"] is None and busy["operation"]["writer_busy"]
    artifact_id = busy["artifact"]["artifact_id"]
    artifact = base + "/context-compactions/" + artifact_id
    canceled = result(api_request(env, "POST", operation_path + "/cancellations", json={}))
    assert canceled["operation"]["status"] == "canceled" and canceled["operation"]["needs_operator_reconciliation"]
    blocked = result(api_request(env, "GET", artifact + "/retention-preview"))
    assert blocked["blockers"] == ["writer_not_drained"] and blocked["writer_busy"]

    async def attest() -> dict[str, Any]:
        engine = create_async_engine(database)
        try:
            async with AsyncSession(engine) as db:
                return await acknowledge_attempt_quiescence(
                    db,
                    memory_id=mid,
                    tenant_id=tenant,
                    operation_id=operation_id,
                    expected_generation=generation,
                    expected_attempt=attempt,
                    expected_active_token=token,
                    evidence_reference=f"isolated in-process fake model, request SIGKILL recorded in journal {journal.run_id}",
                )
        finally:
            await engine.dispose()

    assert asyncio.run(attest())["verification"] == "operator_attestation"
    settled = result(api_request(env, "GET", artifact + "/retention-preview"))
    assert not settled["writer_busy"] and not settled["eligible"] and settled["blockers"] == ["writer_not_drained"]
    denied = api_request(env, "POST", artifact + "/retirements", json={"expected_preview_sha256": settled["preview_sha256"]})
    assert denied.status_code == 409 and denied.json()["data"]["error_code"] == "compaction_retention_not_eligible"
    inventory_path = base + "/context-compactions/inventory"
    inventory = result(api_request(env, "GET", inventory_path))
    assert inventory["retained_count"] == inventory["unsettled_count"] == 1 and inventory["retired_count"] == 0
    _recover_in_web(env, context, work)
    assert result(api_request(env, "GET", inventory_path)) == inventory
    assert asyncio.run(store.inventory()) == raw_before and env["summary_calls"] == []
    with Session(env["engine"]) as db:
        writer = db.get(MemoryWriterAdmission, (mid, operation_id))
        saved = db.get(MemoryCompactionArtifact, artifact_id)
        outbox = db.scalars(publication).one()
        assert writer is not None and writer.attempt == 1 and writer.receipt["outcome"] == "operator_attested_quiescence"
        assert outbox is not None and outbox.receipt["active_attempts"] == []
        assert saved is not None and saved.package is None and saved.retired_at is None
        assert db.scalar(sa.select(sa.func.count()).select_from(MemoryCompactionArtifact).where(MemoryCompactionArtifact.memory_id == mid)) == 1
    journal.event("hard-crash-drain-readback", operation_id=operation_id, raw_inventory_unchanged=True, unproven_slot_still_retained=True, new_model_dispatches=0)
