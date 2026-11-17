"""Pinned Web generation and reopened read-only consumers use real artifact APIs."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.integration.test_memory_message_recovery_web import memory_effects, no_sql_mutations
from tests.support.memory_compaction_http import compaction_api as compaction_api
from tests.support.memory_compaction_http import release_api as release_api
from tests.support.memory_compaction_http import request
from tests.support.memory_compaction_http import writes_api as writes_api
from tests.support.memory_writes_http import api_request, result
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file


def _consumer(env: dict[str, Any], tmp_path: Path, *, label: str, mode: str, state: str) -> dict[str, Any]:
    checkout, sha = pinned_web_checkout()
    journal = ResourceJournal("memory-compaction-web")
    config, receipt = tmp_path / f"{label}.private.json", tmp_path / f"{label}.receipt.json"
    owned: list[Path] = []
    try:
        private_consumer_file(
            config,
            json.dumps(
                {
                    "baseURL": env["base"],
                    "token": env["jwts"][1],
                    "memoryId": env["memory_id"],
                    "operationId": "compaction-web-owned",
                    "sourceIds": env["compaction_source_ids"],
                    "prefixCount": 1,
                    "mode": mode,
                    "expectedState": state,
                    "ownedScratchConfirmed": True,
                    "resultFile": str(receipt),
                }
            ),
            journal,
            owned,
        )
        private_consumer_file(receipt, "", journal, owned)
        journal.event("reserve-compaction-consumer", web_sha=sha, memory_id=env["memory_id"], mode=mode, process_group=os.getpgrp())
        completed = subprocess.run(
            ["node", "--import", "tsx", "scripts/consumers/memory-compaction.ts", str(config)],
            cwd=checkout,
            env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"},
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        value = json.loads(receipt.read_text())
        assert value["passed"] and value["readOnlyRecovery"] and value["applicationRequests"] == 0
        assert value["generationRequests"] == (1 if mode == "generate" else 0)
        assert value["sourceIds"] == env["compaction_source_ids"]
        journal.event("compaction-consumer-readback", web_sha=sha, receipt=value)
        return value
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("compaction-consumer-private-files-cleaned", paths=[str(path) for path in owned], absent=True)


@pytest.mark.external_consumer
def test_pinned_web_generate_reopen_and_stale_read_have_no_hidden_generation(compaction_api: dict[str, Any], tmp_path: Path) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    generated = _consumer(env, tmp_path, label="generate", mode="generate", state="generated")
    assert len(env["summary_calls"]) == 1
    before = memory_effects(env)
    with no_sql_mutations() as writes:
        first = _consumer(env, tmp_path, label="first-read", mode="read", state="generated")
        second = _consumer(env, tmp_path, label="reopened", mode="read", state="generated")
        assert first == second and first["artifactId"] == generated["artifactId"]
        operation_path = f"/memories/{mid}/context-compaction-operations/compaction-web-owned"
        assert api_request(env, "GET", operation_path, actor=2).status_code == 404
        assert api_request(env, "GET", operation_path, token="invalid-session-token").status_code == 401
    assert writes == [] and memory_effects(env) == before and len(env["summary_calls"]) == 1

    source = result(api_request(env, "GET", operation_path))["artifact"]["sources"][1]
    result(
        api_request(
            env,
            "POST",
            f"/memories/{mid}/sources/{source['source_id']}/withdrawals",
            json={"operation_id": "web-withdraw-retained", "expected_source_revision": int(source["source_revision"]), "reason": "Remove retained evidence"},
        )
    )
    before = memory_effects(env)
    with no_sql_mutations() as writes:
        stale = _consumer(env, tmp_path, label="stale-reopened", mode="read", state="stale")
        assert stale["artifactId"] == generated["artifactId"]
        replay = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=request(env, "compaction-web-owned")))
        assert replay["artifact"]["package"] is None
    assert writes == [] and memory_effects(env) == before and len(env["summary_calls"]) == 1
