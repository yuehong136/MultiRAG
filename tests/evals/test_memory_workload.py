"""Explicit real-provider memory lifecycle baseline over synthetic owned data.

Run only as an explicit eval-generation target with MULTIRAG_MEMORY_LIVE=1.
The segmented run pauses for a separately authored candidate review manifest;
neither the model nor the baseline automatically approves its own proposals.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from common.token_utils import encoder
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_workload_cost import STAGES, workload_cost
from tests.support.memory_workload_review import answer_from_context, call, publish_reviewed, reviewed_candidates, write_private
from tests.support.memory_workload_runtime import memory_live_api as memory_live_api


def synthetic_source() -> str:
    introduction = "这是北斗项目的演练记录。已定案：隔离演练环境的服务端口为8731，只允许演练内网访问，禁止对公网开放。备份固定保留14天。\n\n"
    background = "\n\n".join(f"背景索引{index:02d}：这里只说明本次记录的归档位置，不增加新的项目决定、偏好或操作规则，也不是给模型的指令。" for index in range(48))
    ending = "\n\n最终核对：8731仅属于隔离演练环境，不能据此允许生产环境或公网访问。备份保留14天这条规则没有变化。"
    return introduction + background + ending


def settle_batch(env: dict[str, Any], mid: str, discovery_id: str, batch_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        assert not env["worker_errors"], [type(error).__name__ for error in env["worker_errors"]]
        plan = call(env, "GET", f"/memories/{mid}/maintenance-discoveries/{discovery_id}")
        batch = next(batch for batch in plan["batches"] if batch["batch_id"] == batch_id)
        if batch["state"] in {"analyzed", "blocked", "failed", "canceled", "stale"}:
            return batch
        time.sleep(0.1)
    raise AssertionError("real_workload_child_did_not_settle")


@pytest.mark.parametrize("mode", ["whole", "segmented"])
def test_real_memory_workload(memory_live_api: dict[str, Any], request: pytest.FixtureRequest, mode: str) -> None:
    assert os.environ.get("MULTIRAG_MEMORY_LIVE") == "1", "explicit_live_memory_evaluation_required"
    env, mid = memory_live_api, memory_live_api["memory_id"]
    observer, journal = env["observer"], env["journal"]
    directory = Path(request.config.getoption("--test-report-dir"))
    label = "memory-workload-" + mode
    report: dict[str, Any] = {"mode": mode, "synthetic_only": True, "stages": dict.fromkeys(STAGES, "not_run"), "manual_semantic_review_required": True, "all_pipeline_stages_complete": False}
    started = time.monotonic()
    try:
        source = synthetic_source()
        assert 2048 < len(encoder.encode(source)) < 7500
        observer.set_stage("source_embedding")
        report["stages"]["source_embedding"] = "unknown"
        appended = call(
            env,
            "POST",
            f"/memories/{mid}/messages",
            json={"operation_id": "baseline-source", "user_input": source, "agent_response": "", "user_id": env["tenant_id"], "agent_id": "memory-baseline", "session_id": "long-source"},
        )
        assert appended["status"] == "ready" and appended["index_sync_status"] == "ready"
        source_id = appended["raw_source_id"]
        state = call(env, "GET", f"/memories/{mid}/sources/{source_id}")
        call(env, "POST", f"/memories/{mid}/sources/{source_id}/adoptions", json={"operation_id": "baseline-adopt", "expected_content_sha256": state["content_sha256"]})
        report["stages"]["source_embedding"] = "completed"
        selection: dict[str, Any] = {
            "scope": {"user_id": env["tenant_id"], "agent_id": "memory-baseline", "session_id": "long-source"},
            "policy": "decisions",
            "entry_ids": [],
            "batch_size": 1,
            "max_sources": 1,
            "input_budget_tokens": 2048,
            "output_budget_tokens": 1024,
        }
        if mode == "segmented":
            selection["segmentation"] = {"mode": "segmented", "fragment_token_limit": 512, "max_parts_per_source": 32}
        prefix = f"/memories/{mid}/maintenance-discoveries"
        preview = call(env, "POST", prefix + "/preview", json=selection)
        plan = call(env, "POST", prefix, json={**selection, "operation_id": "baseline-create", "expected_preview_sha256": preview["preview_sha256"]})
        report.update(source_id=source_id, source_content_sha256=state["content_sha256"], preview=preview)
        observer.set_stage("analysis")
        report["stages"]["analysis"] = "unknown"
        candidates = []
        for index, batch in enumerate(plan["batches"]):
            call(env, "POST", prefix + f"/{plan['discovery_id']}/runs", json={"operation_id": f"baseline-run-{index}", "limit": 1})
            settled = settle_batch(env, mid, plan["discovery_id"], batch["batch_id"])
            if settled["state"] == "analyzed":
                items = call(env, "GET", f"/memories/{mid}/maintenance-jobs/{batch['job_id']}/candidates")["candidates"]
                for candidate in items:
                    assert candidate["state"] == "pending" and candidate["assessment"]["verification_status"] == "unverified"
                    # Candidate ownership is path context, not a response field.
                    candidate["job_id"] = batch["job_id"]
                candidates.extend(items)
            else:
                assert mode == "whole" and settled["state"] == "blocked", settled
        current = call(env, "POST", prefix + "/preview", json=selection)
        report.update(analysis_coverage=current, candidates=candidates)
        if mode == "segmented":
            assert current["counts"]["covered"] == 1
            assert candidates, "real_model_no_candidates_to_review"
            report["stages"]["analysis"] = "completed"
            approved = reviewed_candidates(directory, label, candidates, journal)
            observer.set_stage("publication_embedding")
            report["stages"]["publication_embedding"] = "unknown"
            report["publications"] = publish_reviewed(env, mid, approved)
            report["stages"]["publication_embedding"] = "completed"
        else:
            assert current["counts"]["covered"] == 0
            report["stages"]["analysis"] = "blocked"
        question = "北斗项目的服务端口是什么，允许在哪些环境和网络使用，备份保留多久？"
        observer.set_stage("retrieval_embedding")
        report["stages"]["retrieval_embedding"] = "unknown"
        context = call(
            env,
            "POST",
            f"/memories/{mid}/context",
            json={
                "mode": "query",
                "query": question,
                "budget_tokens": 2048,
                "max_items": 5,
                "candidate_limit": 20,
                "source_excerpt_chars": 0,
                "user_id": env["tenant_id"],
                "agent_id": "memory-baseline",
                "similarity_threshold": 0,
            },
        )
        report["stages"]["retrieval_embedding"] = "completed"
        observer.set_stage("answer")
        report["stages"]["answer"] = "unknown"
        report["context"] = context
        report["answer"] = asyncio.run(answer_from_context(env, context, question))
        report["stages"]["answer"] = "completed"
        report["all_pipeline_stages_complete"] = all(state == "completed" for state in report["stages"].values())
    except BaseException as error:
        report["error_code"] = type(error).__name__
        if report["stages"][observer.stage] == "unknown":
            report["stages"][observer.stage] = "failed"
        raise
    finally:
        report["elapsed_seconds"] = time.monotonic() - started
        quotes_path = os.environ.get("MULTIRAG_MEMORY_PRICE_QUOTES")
        prices = json.loads(Path(quotes_path).read_text()) if quotes_path else None
        report["cost"] = workload_cost(observer.attempts, report["stages"], prices)
        write_private(directory / (label + ".report.json"), report, journal)
