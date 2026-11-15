"""Pinned Web clients exercise owned appends and deletion on the real HTTP app."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Memory
from api.db.memory_revision_models import ManagedMemoryState, MemoryMutationOperation
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from common import settings
from memory.services.messages import MessageService, index_name
from tests.integration.test_memory_writes_http import manage_bank
from tests.integration.test_memory_writes_http import writes_api as writes_api
from tests.support.agent_update_release import release_api as release_api
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file

pytestmark = pytest.mark.external_consumer


def run_consumer(env: dict[str, Any], tmp_path: Path, script: str) -> dict[str, Any]:
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("memory-writer-web-consumer")
    configuration = tmp_path / "memory-consumer.private.json"
    output = tmp_path / "memory-consumer-result.private.json"
    owned: list[Path] = []
    try:
        private_consumer_file(
            configuration,
            json.dumps(
                {
                    "baseURL": env["base"],
                    "token": env["jwts"][1],
                    "memoryId": env["memory_id"],
                    "ownedScratchConfirmed": True,
                    "resultFile": str(output),
                }
            ),
            journal,
            owned,
        )
        # Pre-create the result with private permissions; Node truncates it.
        private_consumer_file(output, "", journal, owned)
        assert configuration.stat().st_mode & 0o777 == output.stat().st_mode & 0o777 == 0o600
        command = ["node", "--import", "tsx", f"scripts/consumers/{script}.ts", str(configuration)]
        journal.event("reserve-memory-web-consumer", web_sha=web_sha, script=script, memory_id=env["memory_id"], result_file=str(output), process_group=os.getpgrp())
        completed = subprocess.run(command, cwd=checkout, env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"}, text=True, capture_output=True, timeout=180)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        receipt = json.loads(output.read_text())
        assert receipt["passed"] is True
        journal.event("memory-web-consumer-complete", web_sha=web_sha, script=script, receipt=receipt)
        return receipt
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("memory-web-private-files-cleanup", paths=[str(path) for path in owned], absent=True)


def test_pinned_web_raw_append_client(writes_api: dict[str, Any], tmp_path: Path) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    receipt = run_consumer(env, tmp_path, "memory-writes")
    source_id = int(receipt["rawSourceId"])
    assert 2**53 < source_id <= 2**63 - 1
    assert receipt["replayStable"] and receipt["differentBody409"] and receipt["readOnlyOperation"] and receipt["managedModeUnchanged"]
    with Session(env["engine"]) as db:
        assert db.get(ManagedMemoryState, mid) is None
        admission = db.get(MemoryWriterAdmission, (mid, receipt["operationId"]))
        assert admission is not None and admission.publication_state == "ready" and admission.active_token is None
        assert admission.message_ids == [source_id]
    physical = MessageService.get_by_message_id(mid, source_id, env["tenants"]["personal"])
    assert physical is not None and physical["message_type"] == "raw" and physical["status"] is True


def test_pinned_web_managed_deletion_client(writes_api: dict[str, Any], tmp_path: Path) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    manage_bank(env)
    receipt = run_consumer(env, tmp_path, "memory-deletion")
    assert receipt["lifecycle"] == "deleted" and receipt["trackedCleanupConfirmed"] is True
    assert receipt["physicalCleanupComplete"] is False and receipt["receiptReadableAfterDelete"] is True
    with Session(env["engine"]) as db:
        assert db.get(Memory, mid) is None
        bank = db.get(ManagedMemoryState, mid)
        assert bank is not None and bank.state == "deleted" and bank.delete_operation_id == receipt["operationId"]
        assert db.get(MemoryWriterFence, mid).state == "deleted"
        assert db.scalar(sa.select(MemoryMutationOperation.status).where(MemoryMutationOperation.memory_id == mid, MemoryMutationOperation.operation_id == receipt["operationId"])) == "ready"
    assert not settings.msgStoreConn.index_exists_strict(index_name(env["tenants"]["personal"]), mid)
