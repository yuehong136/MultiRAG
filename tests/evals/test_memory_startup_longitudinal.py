"""Explicit real OpenAI startup lifecycle replay with independent synthetic review."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_startup_longitudinal import AGENT, analyze_publish, append, costs, judgment, paired_answers, preview, web_preview
from tests.support.memory_workload_review import call, write_private
from tests.support.memory_workload_runtime import memory_live_api as memory_live_api

CASES = json.loads((Path(__file__).parents[1] / "fixtures/memory_startup_longitudinal.json").read_text())["cases"]


@pytest.mark.external_consumer
@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_real_startup_longitudinal(memory_live_api: dict[str, Any], request: pytest.FixtureRequest, case: dict[str, Any]) -> None:
    assert os.environ.get("MULTIRAG_MEMORY_LIVE_PROVIDER") == "OpenAI"
    env, mid = memory_live_api, memory_live_api["memory_id"]
    directory = Path(request.config.getoption("--test-report-dir"))
    label, subject = case["id"], "startup-owned-subject"
    report: dict[str, Any] = {
        "case_id": label,
        "synthetic_only": True,
        "known_case": True,
        "manual_semantic_review_required": True,
        "model_answers_semantically_verified": False,
        "web": [],
        "phases": {},
        "cost_scope": "all observed setup, embedding, analysis, publication and paired answering calls; independent review and host cost excluded",
    }
    started = time.monotonic()
    try:
        # Verify owner wiring and the pinned read-only client before paid setup.
        assert preview(env, "0" * 32, subject)["items"] == []
        report["web"].append(web_preview(env, "0" * 32, subject, label + "-preflight", False, directory))
        source = append(env, case["source"], label + "-initial-source", subject)
        operations = analyze_publish(env, case, directory, label + "-seed", [source], [])
        entry_id = operations[0]["entries"][0]["entry_id"]
        report.update(source_id=source, entry_id=entry_id, seed_publications=operations, agent_id=AGENT)
        unconfirmed = preview(env, entry_id, subject)
        assert unconfirmed["items"] == []
        report["phases"]["unconfirmed"] = unconfirmed
        report["web"].append(web_preview(env, entry_id, subject, label + "-unconfirmed", False, directory))
        report["initial_confirmation"] = judgment(env, entry_id, "human_confirmed", label + "-confirm", case["applicability"], directory)
        initial = preview(env, entry_id, subject)
        assert [row["entry_id"] for row in initial["items"]] == [entry_id]
        assert initial["items"][0]["assessment"]["verification_status"] == "unverified"
        report["phases"]["initial"] = paired_answers(env, case, subject, initial, label + "-initial")
        report["web"].append(web_preview(env, entry_id, subject, label + "-initial", True, directory))
        if case["change"] == "dispute":
            report["dispute"] = judgment(env, entry_id, "disputed", label + "-dispute", "本合成回放明确暂停该版本的启动供给，等待重新审查；不等于正文已经证明错误。", directory)
        else:
            newer = append(env, case["new_source"], label + "-new-source", subject)
            report["new_source_id"] = newer
            report["revision_publications"] = analyze_publish(env, case, directory, label + "-revision", [source, newer], [entry_id])
        changed = preview(env, entry_id, subject)
        assert changed["items"] == []
        report["phases"]["needs_confirmation"] = changed
        report["web"].append(web_preview(env, entry_id, subject, label + "-changed", False, directory))
        report["new_confirmation"] = judgment(env, entry_id, "human_confirmed", label + "-reconfirm", case["applicability"], directory)
        fresh = preview(env, entry_id, subject)
        assert len(fresh["items"]) == 1 and fresh["package_sha256"] != initial["package_sha256"]
        report["phases"]["fresh"] = paired_answers(env, case, subject, fresh, label + "-fresh")
        report["web"].append(web_preview(env, entry_id, subject, label + "-fresh", True, directory))
        state = call(env, "GET", f"/memories/{mid}/sources/{source}/state")
        report["withdrawal"] = call(
            env,
            "POST",
            f"/memories/{mid}/sources/{source}/withdrawals",
            json={"operation_id": label + "-withdraw", "expected_source_revision": int(state["source_revision"]), "reason": "Owned synthetic final withdrawal; no business records."},
        )
        removed = preview(env, entry_id, subject)
        assert removed["items"] == []
        report["phases"]["withdrawn"] = removed
        report["web"].append(web_preview(env, entry_id, subject, label + "-withdrawn", False, directory))
        report["mechanism_complete"] = True
    except BaseException as error:
        report["error_code"] = type(error).__name__
        report["failed_phase"] = env["observer"].phase
        raise
    finally:
        report["elapsed_seconds_including_review_wait"] = time.monotonic() - started
        report["cost"] = costs(env)
        write_private(directory / ("startup-longitudinal-" + label + ".report.json"), report, env["journal"])
