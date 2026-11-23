"""Opt-in compaction comparison; controlled SDK traffic never reaches a provider."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support import memory_compaction_evaluation as evaluation
from tests.support import memory_compaction_reuse_evaluation as reuse
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_structured_sources_live import require_direct_openai_models
from tests.support.memory_workload_runtime import memory_live_api as memory_live_api

BASELINE = Path(__file__).parents[1] / "fixtures/memory_compaction_baseline.json"
CASES = [("known_baseline", case) for case in evaluation.load_cases(BASELINE, "known_baseline")]
if os.environ.get("MULTIRAG_COMPACTION_HOLDOUT"):
    CASES += [("independent_holdout", case) for case in evaluation.load_cases(Path(os.environ["MULTIRAG_COMPACTION_HOLDOUT"]), "independent_holdout")]
assert len(CASES) <= evaluation.MAX_CASES and len({case["id"] for _, case in CASES}) == len(CASES)


@pytest.mark.parametrize(("case_index", "kind", "case"), [(index, kind, case) for index, (kind, case) in enumerate(CASES)], ids=[case["id"] for _, case in CASES])
def test_real_compaction_comparison(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, case_index: int, kind: str, case: dict[str, Any]) -> None:
    env = memory_live_api
    assert os.environ.get("MULTIRAG_MEMORY_LIVE_PROVIDER") == "OpenAI"
    require_direct_openai_models(env)
    env["journal"].event("compaction-fixture-kind", kind=kind, case_id=case["id"], no_long_term_publication=True, no_context_apply=True)
    report = evaluation.run_case(env, monkeypatch, case, case_index, Path(request.config.getoption("--test-report-dir")))
    assert report["mechanism_complete"] is True


def _cabinet_provenance_case() -> dict[str, Any]:
    fixtures = reuse.load_expansion_cases(BASELINE.with_name("memory_compaction_expansion_holdout.json"), "expansion_holdout")
    frozen = next(case for case in fixtures if case["id"] == "held_cabinet_provenance")
    case, _ = reuse.expansion_trajectory(frozen)
    return case


def test_real_cabinet_provenance_extended_summary_budget(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    env = memory_live_api
    assert os.environ.get("MULTIRAG_MEMORY_LIVE_PROVIDER") == "OpenAI"
    require_direct_openai_models(env)
    case = _cabinet_provenance_case()
    env["journal"].event("compaction-cabinet-provenance-request", case_sha256=evaluation.digest(case), summary_limit=evaluation.EXTENDED_SUMMARY_LIMIT, maximum_attempts=evaluation.MAX_SDK_PER_CASE)
    report = evaluation.run_case(env, monkeypatch, case, 0, Path(request.config.getoption("--test-report-dir")), summary_limit=evaluation.EXTENDED_SUMMARY_LIMIT)
    assert report["planned_scope_complete"] and report["withdrawal_prevents_consumption"]
    assert report["semantic_answers_accepted"] is True


def _install_controlled_sdk(
    env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_summary: bool = False,
    summary_limit: int = evaluation.SUMMARY_LIMIT,
    reasoning_only_summary: bool = False,
) -> list[dict[str, Any]]:
    require_direct_openai_models(env)
    observed: list[dict[str, Any]] = []
    original_sync, original_async = httpx.Client.send, httpx.AsyncClient.send

    def send_sync(client: httpx.Client, outbound: httpx.Request, *args: Any, **options: Any) -> httpx.Response:
        if outbound.url.host != "api.openai.com":
            return original_sync(client, outbound, *args, **options)
        assert outbound.method == "POST" and outbound.url.path.endswith("/embeddings")
        body = json.loads(outbound.content)
        assert body["model"] == "text-embedding-3-small"
        observed.append({"kind": "embedding", "body": body})
        supplied = body["input"] if isinstance(body["input"], list) else [body["input"]]
        assert len(supplied) == 1
        return httpx.Response(
            200,
            request=outbound,
            json={"object": "list", "data": [{"object": "embedding", "index": 0, "embedding": [0.25] * 1536}], "model": body["model"], "usage": {"prompt_tokens": 17, "total_tokens": 17}},
        )

    async def send_async(client: httpx.AsyncClient, outbound: httpx.Request, *args: Any, **options: Any) -> httpx.Response:
        if outbound.url.host != "api.openai.com":
            return await original_async(client, outbound, *args, **options)
        assert outbound.method == "POST" and outbound.url.path.endswith("/chat/completions")
        body = json.loads(outbound.content)
        summary_stage = env["observer"].stage == "analysis"
        expected_limit = summary_limit if summary_stage else evaluation.ANSWER_LIMIT
        assert body["model"] == "gpt-6.1-terra" and body["max_completion_tokens"] == expected_limit and "max_tokens" not in body
        observed.append({"kind": "chat", "body": body})
        if fail_summary and summary_stage:
            return httpx.Response(
                500, request=outbound, json={"error": {"message": "Controlled provider unavailable; never a real provider response.", "type": "server_error", "code": "controlled_unavailable"}}
            )
        if summary_stage:
            text = "仅合成数据与演练内网，禁止生产、真实客户数据和公网；RIV-7F2B-2026每批最多48条，每窗90秒，连续两窗p95均超过280毫秒才停止并回退。文字说明不证明执行或核验。"
        else:
            text = "受控SDK协议响应，不代表实际模型事实准确率。"
        reasoning_only = summary_stage and reasoning_only_summary
        completion_tokens = expected_limit if reasoning_only else 11
        return httpx.Response(
            200,
            request=outbound,
            json={
                "id": "compaction-controlled-sdk",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": None if reasoning_only else text}, "finish_reason": "length" if reasoning_only else "stop"}],
                "usage": {
                    "prompt_tokens": 31,
                    "completion_tokens": completion_tokens,
                    "total_tokens": 31 + completion_tokens,
                    "prompt_tokens_details": {"cached_tokens": 0},
                    "completion_tokens_details": {"reasoning_tokens": completion_tokens if reasoning_only else 0},
                },
            },
        )

    def simulated_review(directory: Path, label: str, value: dict[str, Any], journal: Any, **options: Any) -> dict[str, Any]:
        if value["kind"] == "answers":
            assert all(set(item) == {"sources", "question", "answer", "rubric"} for item in value["review_items"].values())
        decision = {
            "request_sha256": evaluation.digest(value),
            "reviewed_by": "controlled_protocol_simulation_only",
            "synthetic_only": True,
            "judgments": {
                key: {"required": {row["id"]: "satisfied" for row in item["rubric"]["required"]}, "forbidden": {row["id"]: "satisfied" for row in item["rubric"]["forbidden"]}, "accepted": True}
                for key, item in value["review_items"].items()
            },
            "notes": "Simulated decisions test protocol only, never factual quality or actual human adoption.",
        }
        return evaluation.validate_judgment(value, decision)

    monkeypatch.setattr(httpx.Client, "send", send_sync)
    monkeypatch.setattr(httpx.AsyncClient, "send", send_async)
    monkeypatch.setattr(evaluation, "external_review", simulated_review)
    return observed


def test_controlled_compaction_sdk_flow(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Real API/SQL/Milvus/prepared SDK with fake OpenAI HTTP and human decisions."""
    env = memory_live_api
    observed = _install_controlled_sdk(env, monkeypatch)
    case = evaluation.load_cases(BASELINE, "known_baseline")[0]
    report = evaluation.run_case(env, monkeypatch, case, 0, Path(request.config.getoption("--test-report-dir")))
    assert report["mechanism_complete"] is True and len(observed) == 5
    assert len(env["observer"].attempts) == 5
    assert [row["kind"] for row in observed].count("embedding") == 2
    env["journal"].event("controlled-compaction-sdk-complete", external_provider_calls=0, sdk_calls=5, usage_simulated=True, human_review_simulated=True, not_real_quality=True)


def test_controlled_compaction_sdk_failure_does_not_retry(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """A failed synthetic SDK call retains unknown usage and never reaches answers."""
    env = memory_live_api
    observed = _install_controlled_sdk(env, monkeypatch, fail_summary=True)
    case = evaluation.load_cases(BASELINE, "known_baseline")[0]
    case["id"] += "_failure"
    directory = Path(request.config.getoption("--test-report-dir"))
    with pytest.raises(AssertionError):
        evaluation.run_case(env, monkeypatch, case, 0, directory)
    report = json.loads((directory / (case["id"] + ".compaction-report.json")).read_text())
    assert report["phase"] == "summary_generation" and report["mechanism_complete"] is False
    assert report["consumer_adopted_for_evaluation_only"] is False and "answers" not in report
    attempts = env["observer"].attempts
    assert len(attempts) == len(observed) == 3 and [row["kind"] for row in observed].count("chat") == 1
    assert attempts[-1]["state"] == "failed" and attempts[-1]["provider_total_tokens"] is None
    assert report["generated"]["operation"]["status"] == "failed" and report["generated"]["artifact"]["package"] is None
    env["journal"].event("controlled-compaction-sdk-failure", external_provider_calls=0, sdk_calls=3, summary_attempts=1, answer_calls=0, failed_usage_unknown=True, retries=0)


@pytest.mark.parametrize("reasoning_only", [False, True], ids=["complete", "reasoning-only"])
def test_controlled_cabinet_provenance_extended_summary_budget(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, reasoning_only: bool) -> None:
    case = _cabinet_provenance_case()
    case["id"] += "-controlled-empty" if reasoning_only else "-controlled-complete"
    directory = Path(request.config.getoption("--test-report-dir"))
    limit = evaluation.EXTENDED_SUMMARY_LIMIT
    assert evaluation.preflight_case(case)["completion_reserve_tokens"] == 1536
    observed = _install_controlled_sdk(memory_live_api, monkeypatch, summary_limit=limit, reasoning_only_summary=reasoning_only)
    if reasoning_only:
        with pytest.raises(AssertionError):
            evaluation.run_case(memory_live_api, monkeypatch, case, 0, directory, summary_limit=limit)
        report = json.loads((directory / (case["id"] + ".compaction-report.json")).read_text())
        assert report["generated"]["operation"]["error_code"] == "empty_compaction_summary"
        assert report["generated"]["artifact"]["package"] is None and "answers" not in report
        assert report["consumer_adopted_for_evaluation_only"] is False
        assert report["actual_attempts"][-1]["finish_reason"] == "length"
    else:
        report = evaluation.run_case(memory_live_api, monkeypatch, case, 0, directory, summary_limit=limit)
        assert report["planned_scope_complete"] and report["withdrawal_prevents_consumption"]
        assert report["semantic_answers_accepted"] is True
    assert report["preflight"]["completion_reserve_tokens"] == 2048
    assert [row["body"]["max_completion_tokens"] for row in observed if row["kind"] == "chat"] == ([1024] if reasoning_only else [1024, 512, 512])
    assert len(report["actual_attempts"]) == len(observed) == (3 if reasoning_only else 5)
