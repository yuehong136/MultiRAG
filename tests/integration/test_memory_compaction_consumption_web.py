"""Copied Web references prepare fresh requests without hidden generation."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from common.token_utils import encoder
from memory.compaction.consumption import prepare_compaction_request
from tests.support.memory_compaction_http import compaction_api as compaction_api
from tests.support.memory_compaction_http import release_api as release_api
from tests.support.memory_compaction_http import request
from tests.support.memory_compaction_http import writes_api as writes_api
from tests.support.memory_message_recovery_web import memory_effects, no_sql_mutations
from tests.support.memory_writes_http import api_request, result
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file


def copied_reference(env: dict[str, Any], tmp_path: Path, *, label: str, state: str) -> dict[str, str] | None:
    checkout, sha = pinned_web_checkout()
    journal = ResourceJournal("memory-compaction-reference-web")
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
                    "operationId": "consume-web-reference",
                    "sourceIds": env["compaction_source_ids"],
                    "prefixCount": 1,
                    "mode": "read",
                    "expectedState": state,
                    "exportReference": True,
                    "ownedScratchConfirmed": True,
                    "resultFile": str(receipt),
                }
            ),
            journal,
            owned,
        )
        private_consumer_file(receipt, "", journal, owned)
        journal.event("reserve-compaction-reference-consumer", web_sha=sha, memory_id=env["memory_id"], process_group=os.getpgrp())
        completed = subprocess.run(
            ["node", "--import", "tsx", "scripts/consumers/memory-compaction.ts", str(config)],
            cwd=checkout,
            env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"},
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if state != "generated" and completed.returncode != 0:
            assert receipt.read_text() == ""
            journal.event("stale-reference-export-rejected", web_sha=sha, raw_exit=completed.returncode)
            return None
        assert completed.returncode == 0, completed.stdout + completed.stderr
        value = json.loads(receipt.read_text())
        assert value["generationRequests"] == value["applicationRequests"] == 0 and value["readOnlyRecovery"]
        reference = value.get("reference")
        if state == "generated":
            assert isinstance(reference, dict) and set(reference) == {"memory_id", "operation_id", "artifact_id", "input_manifest_sha256"}
        else:
            assert reference is None
        journal.event("copied-reference-readback", web_sha=sha, reference=reference, state=state)
        return reference
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("compaction-reference-private-files-cleaned", paths=[str(path) for path in owned], absent=True)


@pytest.mark.external_consumer
def test_copied_reference_prepares_twice_and_withdrawal_stops_new_consumption(compaction_api: dict[str, Any], tmp_path: Path) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    generated = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=request(env, "consume-web-reference")))
    artifact = generated["artifact"]
    expected = {key: artifact[key] for key in ("memory_id", "operation_id", "artifact_id", "input_manifest_sha256")}
    preparations: list[str] = []

    def fresh(memory_id: str, operation_id: str) -> dict[str, Any]:
        assert memory_id == mid and operation_id == expected["operation_id"]
        preparations.append(operation_id)
        return result(api_request(env, "GET", f"/memories/{mid}/context-compaction-operations/{operation_id}"))

    options: dict[str, Any] = {
        "scope": {"user_id": env["owners"][1], "agent_id": "compaction-agent", "session_id": None},
        "model": "gpt-6.1-terra",
        "question": "What conditions apply to this synthetic evaluation?",
        "context_window_tokens": 16384,
        "count_tokens": lambda text: len(encoder.encode(text, disallowed_special=())),
        "tokenizer_label": "cl100k_base_estimate",
    }
    before = memory_effects(env)
    with no_sql_mutations() as writes:
        for index in range(2):
            reference = copied_reference(env, tmp_path, label=f"fresh-{index}", state="generated")
            assert reference == expected
            assert reference is not None
            prepared = prepare_compaction_request(reference, fresh, **options)
            assert prepared["reference"] == expected and prepared["snapshot_only"]
            assert json.loads(prepared["request"]["messages"][1]["content"]) == artifact["package"]
            assert prepared["request"]["max_completion_tokens"] == 512
    assert writes == [] and memory_effects(env) == before and len(preparations) == 2 and len(env["summary_calls"]) == 1

    source = artifact["sources"][1]
    result(
        api_request(
            env,
            "POST",
            f"/memories/{mid}/sources/{source['source_id']}/withdrawals",
            json={"operation_id": "withdraw-copied-reference", "expected_source_revision": int(source["source_revision"]), "reason": "Withdraw retained evidence before the next consumption"},
        )
    )
    before = memory_effects(env)
    with no_sql_mutations() as writes:
        assert copied_reference(env, tmp_path, label="after-withdrawal", state="stale") is None
        with pytest.raises(ValueError):
            prepare_compaction_request(expected, fresh, **options)
    assert writes == [] and memory_effects(env) == before and len(preparations) == 3 and len(env["summary_calls"]) == 1
