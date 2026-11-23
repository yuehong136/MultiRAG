"""Opt-in direct-provider source provenance flow; independent human decisions gate writes."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_startup_longitudinal import costs
from tests.support.memory_structured_sources_live import CASES, analyze_and_publish, append_declared_source, confirm_for_startup, require_direct_openai_models, startup
from tests.support.memory_workload_review import call, write_private
from tests.support.memory_workload_runtime import memory_live_api as memory_live_api
from tests.support.memory_writes_http import api_request


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_real_structured_source_lifecycle(memory_live_api: dict[str, Any], request: pytest.FixtureRequest, case: dict[str, Any]) -> None:
    assert os.environ.get("MULTIRAG_MEMORY_LIVE_PROVIDER") == "OpenAI"
    env, mid, label = memory_live_api, memory_live_api["memory_id"], case["id"]
    require_direct_openai_models(env)
    env["observer"].max_attempts = 16
    directory = Path(request.config.getoption("--test-report-dir"))
    subject = "structured-synthetic-subject"
    report: dict[str, Any] = {
        "case_id": label,
        "synthetic_only": True,
        "known_case": True,
        "manual_review_required": True,
        "tool_executed": False,
        "speaker_identity_verified": False,
        "content_objectively_verified": False,
        "answer_model_called": False,
        "mechanism_complete": False,
        "cost_scope": "All observed source/publication embedding and analysis calls, including initialization and failures. No answer calls; independent reviewers and host cost excluded.",
    }
    started = time.monotonic()
    report["source_attempts"] = env.setdefault("structured_source_attempts", [])
    report["analysis_progress"] = env.setdefault("structured_analysis_progress", {})
    report["verification_progress"] = env.setdefault("structured_verification_progress", [])
    try:
        sources: list[dict[str, Any]] = []
        report["sources"] = sources
        for index, source in enumerate(case["sources"]):
            sources.append(append_declared_source(env, source, label=label + "-source-" + str(index), subject=subject))
        assert all("gpt-6.1-terra" not in attempt["model_reference"] for attempt in env["observer"].attempts), "RAW submission must not trigger chat extraction"
        analyzed = analyze_and_publish(env, case, sources, directory)
        report["analysis_and_publication"] = analyzed
        entries = analyzed["entries"]
        ids = [entry["entry_id"] for entry in entries]
        assert entries and len(set(ids)) == len(ids)
        assert startup(env, ids, subject)["items"] == [], "Publication alone does not provide a valid human confirmation"
        report["verifications"] = confirm_for_startup(env, case, entries, directory)
        packet = startup(env, ids, subject)
        assert {item["entry_id"] for item in packet["items"]} == set(ids)
        assert all(item["verification"]["applicability"] == case["applicability"] and item["assessment"]["verification_status"] == "unverified" for item in packet["items"])
        report["startup"] = packet
        for entry in entries:
            assert all(ref["locator"].get("source_provenance_sha256") for ref in entry["current"]["sources"])
        withdrawn_source = str(entries[0]["current"]["sources"][0]["source_id"])
        state = call(env, "GET", f"/memories/{mid}/sources/{withdrawn_source}/state")
        report["withdrawal"] = call(
            env,
            "POST",
            f"/memories/{mid}/sources/{withdrawn_source}/withdrawals",
            json={"operation_id": label + "-withdraw", "expected_source_revision": int(state["source_revision"]), "reason": "Owned synthetic source withdrawal; no business records."},
        )
        assert report["withdrawal"]["stop_supply"] == "withdrawn"
        removed = startup(env, [entries[0]["entry_id"]], subject)
        assert removed["items"] == []
        report["startup_after_withdrawal"] = removed
        unavailable = api_request(env, "GET", f"/memories/{mid}/sources/{withdrawn_source}/provenance", actor=0, params={"include_segments": True})
        assert unavailable.status_code in {404, 409}, unavailable.text
        assert entries[0]["current"]["content"] not in unavailable.text
        withdrawn_record = next(row for row in sources if row["source_id"] == withdrawn_source)
        assert all(segment["content"] not in unavailable.text for segment in withdrawn_record["provenance"]["segments"])
        report["provenance_after_withdrawal"] = {"status_code": unavailable.status_code, "no_entry_body": True}
        report["mechanism_complete"] = True
    except BaseException as error:
        report["error_code"], report["failed_phase"] = type(error).__name__, env["observer"].phase
        raise
    finally:
        report["elapsed_seconds_including_independent_review"] = time.monotonic() - started
        report["cost"] = costs(env)
        write_private(directory / ("structured-sources-" + label + ".report.json"), report, env["journal"])
