"""Run the documented module entry against a real authenticated artifact API."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.support.memory_compaction_http import compaction_api as compaction_api
from tests.support.memory_compaction_http import release_api as release_api
from tests.support.memory_compaction_http import request
from tests.support.memory_compaction_http import writes_api as writes_api
from tests.support.memory_message_recovery_web import memory_effects, no_sql_mutations
from tests.support.memory_writes_http import api_request, result
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import private_consumer_file


def test_module_preview_uses_current_api_and_rejects_withdrawal_or_wrong_actor(compaction_api: dict[str, Any], tmp_path: Path) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    generated = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=request(env, "cli-reference")))
    artifact = generated["artifact"]
    reference = {key: artifact[key] for key in ("memory_id", "operation_id", "artifact_id", "input_manifest_sha256")}
    path = tmp_path / "consumer-reference.private.json"
    journal = ResourceJournal("memory-compaction-cli")
    owned: list[Path] = []
    private_consumer_file(path, json.dumps(reference), journal, owned)

    def run(actor: int = 1) -> subprocess.CompletedProcess[str]:
        journal.event("reserve-cli-preview", reference_path=str(path), actor_index=actor, process_group=os.getpgrp(), no_model_send=True)
        return subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "examples.python.memory_compaction_consumer",
                str(path),
                "--user-id",
                env["owners"][1],
                "--agent-id",
                "compaction-agent",
                "--question",
                "What scope applies?",
            ],
            cwd=Path(__file__).parents[2],
            env={**os.environ, "MULTIRAG_API_BASE": env["base"] + "/api/v1", "MULTIRAG_API_TOKEN": env["jwts"][actor], "OPENAI_API_KEY": "preview-must-not-send"},
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    try:
        before = memory_effects(env)
        with no_sql_mutations() as writes:
            preview = run()
            assert preview.returncode == 0, preview.stderr
            value = json.loads(preview.stdout)
            assert set(value) == {"reference", "budget", "snapshot_only", "sent"}
            assert value["reference"] == reference and value["sent"] is False and value["snapshot_only"] is True
            assert value["budget"]["required_tokens"] <= value["budget"]["context_window_tokens"]
            assert value["budget"]["token_count_is_estimate"] is True
            denied = run(actor=2)
            assert denied.returncode == 1 and denied.stdout == ""
            assert "Consumption stopped: HTTPStatusError" in denied.stderr and env["jwts"][2] not in denied.stderr
        assert writes == [] and memory_effects(env) == before and len(env["summary_calls"]) == 1
        source = artifact["sources"][1]
        result(
            api_request(
                env,
                "POST",
                f"/memories/{mid}/sources/{source['source_id']}/withdrawals",
                json={"operation_id": "cli-withdraw", "expected_source_revision": int(source["source_revision"]), "reason": "Withdraw retained evidence before rerunning preview"},
            )
        )
        before = memory_effects(env)
        with no_sql_mutations() as writes:
            stale = run()
            assert stale.returncode == 1 and stale.stdout == "" and "Consumption stopped: ValueError" in stale.stderr
        assert writes == [] and memory_effects(env) == before and len(env["summary_calls"]) == 1
        journal.event("cli-preview-readback", preview_exit=0, denied_exit=1, stale_exit=1, model_sends=0)
    finally:
        for file in owned:
            file.unlink(missing_ok=True)
            assert not file.exists()
        journal.event("cli-preview-reference-cleaned", paths=[str(file) for file in owned], absent=True)
