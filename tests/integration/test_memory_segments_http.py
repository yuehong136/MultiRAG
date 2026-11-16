"""Fragment coverage and immutable provenance through real owned HTTP/storage."""

import asyncio
import json
import os
import subprocess
import threading
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.memory_incremental_models import MemoryMaintenanceDiscovery, MemoryMaintenanceFragmentCoverage
from api.db.memory_maintenance_models import MemoryMaintenanceCandidate
from memory.revisions.domain import content_hash
from tests.integration.test_memory_dreaming_http import wait_job
from tests.support.memory_incremental_http import create, endpoint, preview, run, seed_source, selection, wait_plan
from tests.support.memory_incremental_http import dreaming_api as dreaming_api
from tests.support.memory_incremental_http import incremental_api as incremental_api
from tests.support.memory_incremental_http import release_api as release_api
from tests.support.memory_incremental_http import writes_api as writes_api
from tests.support.memory_writes_http import WriteChat, api_request, result
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file

_SOURCE = 905
_SCOPE = {"user_id": "existing-subject", "agent_id": "existing-agent", "session_id": "source-segments"}


@pytest.fixture
def segment_api(incremental_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    env = incremental_api
    text = (
        "条件：只能在测试内网使用，不准公网开放。\r\n\r\n"
        + "\n\n".join(f"第{index}段保存独立条件与兄弟事实，emoji🚀和组合字e\u0301保持原文。" for index in range(80))
        + "\n\n末尾：检查码LATE-742，备份仍保留14天。"
    )
    seed_source(env, _SOURCE, text, session=_SCOPE["session_id"])
    env["segment_text"] = text

    async def chat(self: WriteChat, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> tuple[str, int]:
        payload = json.loads(history[-1]["content"])
        env["analysis_calls"].append(payload)
        if "analysis_started" in env:
            env["analysis_started"].set()
            await asyncio.to_thread(env["analysis_release"].wait, 30)
        fragment = next(item for item in payload["inputs"] if item.get("fragment"))
        span = fragment["fragment"]
        assert fragment["snapshot"]["content"] == text[span["read_start"] : span["read_end"]]
        assert fragment["content_sha256"] == content_hash(text)
        if span["part_index"] == env.get("invalid_part"):
            return json.dumps(
                {
                    "proposals": [
                        {
                            "action": "patch",
                            "target_entry_id": "f" * 32,
                            "patch": {"old_text": "missing", "new_text": "invented"},
                            "source_input_ids": [fragment["input_id"]],
                            "reason": "invalid target",
                        }
                    ]
                }
            ), 0
        proposals = (
            []
            if span["part_index"] == env.get("empty_part")
            else [
                {
                    "action": "add",
                    "message_type": "semantic",
                    "content": f"Unverified fragment observation {span['part_index']}.",
                    "source_input_ids": [fragment["input_id"]],
                    "reason": "Synthetic fragment provenance for explicit review.",
                }
            ]
        )
        return json.dumps({"proposals": proposals}), 0

    monkeypatch.setattr(WriteChat, "async_chat", chat)
    return env


def segmented(**overrides: Any) -> dict[str, Any]:
    return selection(
        scope=dict(_SCOPE), batch_size=1, input_budget_tokens=4096, output_budget_tokens=512, segmentation={"mode": "segmented", "fragment_token_limit": 512, "max_parts_per_source": 32}, **overrides
    )


def finish(env: dict[str, Any], plan: dict[str, Any], *, prefix: str) -> dict[str, Any]:
    for index, batch in enumerate(plan["batches"]):
        current = result(api_request(env, "GET", endpoint(env, "/" + plan["discovery_id"])))
        state = next(item for item in current["batches"] if item["batch_id"] == batch["batch_id"])["state"]
        if state in {"analyzed", "failed", "blocked", "stale", "canceled"}:
            continue
        run(env, plan, operation=f"{prefix}-{index}", limit=1)
        wait_plan(
            env, plan["discovery_id"], lambda value: next(item for item in value["batches"] if item["batch_id"] == batch["batch_id"])["state"] in {"analyzed", "failed", "blocked", "stale", "canceled"}
        )
    return result(api_request(env, "GET", endpoint(env, "/" + plan["discovery_id"])))


def test_fragment_preview_is_readonly_and_complete_source_waits_for_every_part(segment_api: dict[str, Any]) -> None:
    env, request = segment_api, segmented()
    observed = preview(env, request)
    assert observed["token_fit"] == "checked_at_preview" and observed["counts"]["selected_primary"] == 1
    sources = [item for batch in observed["batches"] for item in batch["primary_sources"]]
    spans = [item["fragment"] for item in sources]
    assert len(spans) >= 3
    assert "".join(env["segment_text"][span["core_start"] : span["core_end"]] for span in spans) == env["segment_text"]
    assert spans[0]["core_start"] == 0 and spans[-1]["core_end"] == len(env["segment_text"])
    assert all(left["core_end"] == right["core_start"] for left, right in pairwise(spans))
    assert all(span["offset_unit"] == "unicode_codepoint" and span["read_start"] <= span["core_start"] < span["core_end"] <= span["read_end"] for span in spans)
    assert not env["analysis_calls"]
    with Session(env["engine"]) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceDiscovery).where(MemoryMaintenanceDiscovery.memory_id == env["memory_id"])) == 0
    assert api_request(env, "POST", endpoint(env, "/preview"), actor=2, json=request).status_code == 404
    plan = create(env, request)
    env["empty_part"] = 0
    run(env, plan, operation="first-fragment", limit=1)
    first = wait_plan(env, plan["discovery_id"], lambda row: row["progress"]["analyzed"] == 1)
    assert first["source_coverage"]["covered"] == 0 and first["source_coverage"]["partial"] == 1
    assert first["source_coverage"]["analyzed_parts"] == 1
    run(env, plan, operation="first-fragment", limit=1)
    assert len(env["analysis_calls"]) == 1
    ended = finish(env, plan, prefix="remaining-fragment")
    assert ended["source_coverage"]["covered"] == 1
    assert ended["source_coverage"]["analyzed_parts"] == len(spans)
    assert len(env["analysis_calls"]) == len(spans)
    assert preview(env, request)["counts"]["covered"] == 1
    for batch in ended["batches"]:
        candidates = result(api_request(env, "GET", f"/memories/{env['memory_id']}/maintenance-jobs/{batch['job_id']}/candidates"))["candidates"]
        assert all(candidate["state"] == "pending" and candidate["assessment"]["verification_status"] == "unverified" for candidate in candidates)
        assert all(candidate["sources"][0]["fragment"]["fragment_id"] in {span["fragment_id"] for span in spans} for candidate in candidates)


def test_invalid_fragment_stays_uncovered_and_new_plan_reuses_confirmed_parts(segment_api: dict[str, Any]) -> None:
    env, request = segment_api, segmented()
    env["invalid_part"] = 0
    first = create(env, request)
    ended = finish(env, first, prefix="bad-fragment")
    assert ended["source_coverage"]["covered"] == 0
    assert ended["source_coverage"]["analyzed_parts"] == len(first["batches"]) - 1
    old_calls = len(env["analysis_calls"])
    env.pop("invalid_part")
    next_plan = create(env, request, operation="resume-fragments")
    assert len(next_plan["batches"]) == 1
    assert next_plan["batches"][0]["primary_sources"][0]["fragment"]["part_index"] == 0
    completed = finish(env, next_plan, prefix="recovered-fragment")
    assert completed["source_coverage"]["covered"] == 1
    assert preview(env, request)["counts"]["covered"] == 1
    assert len(env["analysis_calls"]) == old_calls + 1


def test_source_withdrawal_fences_fragment_result_and_cleans_pending_candidates(segment_api: dict[str, Any]) -> None:
    env = segment_api
    plan = create(env, segmented())
    env["analysis_started"], env["analysis_release"] = threading.Event(), threading.Event()
    try:
        run(env, plan, limit=1)
        assert env["analysis_started"].wait(20)
        response = api_request(
            env,
            "POST",
            f"/memories/{env['memory_id']}/sources/{_SOURCE}/withdrawals",
            json={"operation_id": "withdraw-segment-source", "expected_source_revision": 1, "reason": "Synthetic source withdrawn during IO"},
        )
        assert response.status_code == 202
    finally:
        env["analysis_release"].set()
    assert wait_job(env, plan["batches"][0]["job_id"])["state"] == "redacted"
    assert preview(env, segmented())["counts"]["covered"] == 0
    with Session(env["engine"]) as reader:
        assert (
            reader.scalar(
                sa.select(sa.func.count())
                .select_from(MemoryMaintenanceFragmentCoverage)
                .where(MemoryMaintenanceFragmentCoverage.memory_id == env["memory_id"], MemoryMaintenanceFragmentCoverage.state == "analyzed")
            )
            == 0
        )
        assert reader.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCandidate).where(MemoryMaintenanceCandidate.memory_id == env["memory_id"])) == 0
    assert result(api_request(env, "POST", f"/memories/{env['memory_id']}/operations/withdraw-segment-source/reconciliations"))["index_sync_status"] == "ready"


def test_fragment_part_limit_is_explicit_and_never_becomes_coverage(segment_api: dict[str, Any]) -> None:
    env = segment_api
    request = segmented()
    request["segmentation"]["max_parts_per_source"] = 1
    observed = preview(env, request)
    assert observed["blocked_sources"][0]["error_code"] == "segment_parts_limit"
    assert observed["source_coverage"]["covered"] == 0
    assert observed["counts"]["selected_fragments"] == 0
    assert not env["analysis_calls"]


@pytest.mark.external_consumer
def test_pinned_web_fragment_progress_and_recovery(segment_api: dict[str, Any], tmp_path: Path) -> None:
    env = segment_api
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("memory-segments-web-consumer")
    configuration, output = tmp_path / "segments-consumer.private.json", tmp_path / "segments-consumer-result.json"
    owned: list[Path] = []
    try:
        private_consumer_file(
            configuration,
            json.dumps(
                {
                    "baseURL": env["base"],
                    "token": env["jwts"][1],
                    "memoryId": env["memory_id"],
                    "expectedSourceId": str(_SOURCE),
                    "scope": _SCOPE,
                    "fragmentTokenLimit": 512,
                    "expectedMinParts": 3,
                    "expectedCodepoints": len(env["segment_text"]),
                    "ownedScratchConfirmed": True,
                    "resultFile": str(output),
                }
            ),
            journal,
            owned,
        )
        private_consumer_file(output, "", journal, owned)
        journal.event("reserve-segment-web-consumer", web_sha=web_sha, memory_id=env["memory_id"], script="scripts/consumers/memory-segments.ts")
        completed = subprocess.run(
            ["node", "--import", "tsx", "scripts/consumers/memory-segments.ts", str(configuration)],
            cwd=checkout,
            env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"},
            text=True,
            capture_output=True,
            timeout=240,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        receipt = json.loads(output.read_text())
        assert receipt["passed"] is True
        journal.event("segments-web-consumer-readback", web_sha=web_sha, receipt=receipt)
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("segments-web-private-files-cleaned", paths=[str(path) for path in owned], absent=True)
