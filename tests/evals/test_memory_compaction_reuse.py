"""Opt-in repeated consumption; one trajectory, no repeated 1/2/4 experiments."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support import memory_compaction_evaluation as original
from tests.support import memory_compaction_reuse_evaluation as reuse
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_structured_sources_live import require_direct_openai_models
from tests.support.memory_workload_runtime import memory_live_api as memory_live_api

BASELINE = Path(__file__).parents[1] / "fixtures/memory_compaction_baseline.json"
CASES = [("known_baseline", case) for case in original.load_cases(BASELINE, "known_baseline")]
if os.environ.get("MULTIRAG_COMPACTION_REUSE_HOLDOUT"):
    CASES += [("independent_holdout", case) for case in original.load_cases(Path(os.environ["MULTIRAG_COMPACTION_REUSE_HOLDOUT"]), "independent_holdout")]
assert len(CASES) <= 2 and len({case["id"] for _, case in CASES}) == len(CASES)


def expansion_cases() -> list[tuple[int, str, dict[str, Any]]]:
    paths = {"expansion_development": os.environ.get("MULTIRAG_COMPACTION_EXPANSION_DEV"), "expansion_holdout": os.environ.get("MULTIRAG_COMPACTION_EXPANSION_HOLDOUT")}
    if not any(paths.values()):
        return []
    cases: list[tuple[str, dict[str, Any]]] = []
    for kind, path in paths.items():
        if not path:
            raise ValueError("compaction_expansion_requires_development_and_holdout_fixtures")
        cases.extend((kind, case) for case in reuse.load_expansion_cases(Path(path), kind))
    historical_ids = {case["id"] for _, case in CASES}
    for name in ("memory_compaction_holdout.json", "memory_compaction_reuse_holdout.json"):
        historical_ids.update(case["id"] for case in original.load_cases(BASELINE.with_name(name), "independent_holdout"))
    new_ids = {case["id"] for _, case in cases}
    if len(new_ids) != reuse.EXPANSION_CASES or new_ids & historical_ids:
        raise ValueError("compaction_expansion_requires_eight_new_unique_cases")
    return [(index, kind, case) for index, (kind, case) in enumerate(cases)]


@pytest.mark.parametrize(("case_index", "kind", "case"), [(index, kind, case) for index, (kind, case) in enumerate(CASES)], ids=[case["id"] for _, case in CASES])
def test_real_compaction_reuse(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, case_index: int, kind: str, case: dict[str, Any]) -> None:
    env = memory_live_api
    assert os.environ.get("MULTIRAG_MEMORY_LIVE_PROVIDER") == "OpenAI"
    require_direct_openai_models(env)
    env["journal"].event("reuse-fixture-kind", kind=kind, case_id=case["id"], one_summary=True, same_question_repeats=4, SDK_maximum=11)
    report = reuse.run_case(env, monkeypatch, case, case_index, Path(request.config.getoption("--test-report-dir")))
    assert report["mechanism_complete"] is True


EXPANSION_CASES = expansion_cases()


def fidelity_cases() -> list[tuple[int, str, dict[str, Any]]]:
    if os.environ.get("MULTIRAG_COMPACTION_FIDELITY") != "1":
        return []
    previous_holdout = reuse.load_expansion_cases(BASELINE.with_name("memory_compaction_expansion_holdout.json"), "expansion_holdout")
    provenance = next(case for case in previous_holdout if case["id"] == "held_cabinet_provenance")
    development = reuse.load_expansion_cases(BASELINE.with_name("memory_compaction_expansion.json"), "expansion_development")
    renderer = next(case for case in development if case["id"] == "renderer-zero-and-empty")
    background_questions = (
        "白椋后来只切换预览背景。这次改动有没有改变闲置保持、主动推送或外部受众？请给出当前三个设置、含义和适用范围。",
        "复核白椋换背景后的三个配置。哪些功能仍关闭？外部账户白名单有谁？请给出准确配置值和范围，并说明背景修改是否改变它们。",
        "如果只发生白椋已经记录的那次视觉改动，闲置保持、主动推送和外部受众各保持什么设置？逐项列出原值、含义和适用范围。",
    )
    renderer.update(reuse_kind="longitudinal", followups=[{"question": question, "rubric": renderer["rubric"]["answer"]} for question in background_questions])
    cases = [(0, "fixed_provenance", provenance), (1, "fixed_retained", renderer)]
    if holdout_path := os.environ.get("MULTIRAG_COMPACTION_FIDELITY_HOLDOUT"):
        payload = json.loads(Path(holdout_path).read_text())
        assert isinstance(payload, dict) and set(payload) == {"schema_version", "kind", "cases"}
        assert type(payload["schema_version"]) is int and payload["schema_version"] == 1 and payload["kind"] == "fidelity_holdout"
        assert isinstance(payload["cases"], list) and len(payload["cases"]) == 1
        sealed = payload["cases"][0]
        checked, _ = reuse.expansion_trajectory(sealed)
        historical = [case for _, case in CASES] + development + previous_holdout
        assert all(checked["id"] != case["id"] and checked["sources"] != case["sources"] for case in historical)
        cases.append((2, "independent_holdout", sealed))
    return cases


@pytest.mark.parametrize(("case_index", "kind", "case"), EXPANSION_CASES, ids=[case["id"] for _, _, case in EXPANSION_CASES])
def test_real_compaction_expansion(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, case_index: int, kind: str, case: dict[str, Any]) -> None:
    env = memory_live_api
    assert os.environ.get("MULTIRAG_MEMORY_LIVE_PROVIDER") == "OpenAI"
    require_direct_openai_models(env)
    quote_path = os.environ.get("MULTIRAG_COMPACTION_EXPANSION_PRICES") or os.environ.get("MULTIRAG_MEMORY_PRICE_QUOTES")
    prices = json.loads(Path(quote_path).read_text()) if quote_path else None
    if quote_path:
        monkeypatch.setenv("MULTIRAG_MEMORY_PRICE_QUOTES", quote_path)
    env["journal"].event("reuse-fixture-kind", kind=kind, case_id=case["id"], reuse_kind=case["reuse_kind"], one_summary=True, SDK_maximum=reuse.MAX_SDK_PER_CASE)
    report = reuse.run_case(env, monkeypatch, case, case_index, Path(request.config.getoption("--test-report-dir")), prices=prices)
    assert report["mechanism_complete"] is True
    assert len(report["actual_attempts"]) == reuse.MAX_SDK_PER_CASE
    assert report["semantic_prefixes"]["4"]["complete_pairs"] is True
    assert report["cumulative_cost"]["unattributed_answer_attempts"]["attempt_count"] == 0


FIDELITY_CASES = fidelity_cases()


@pytest.mark.parametrize(("case_index", "kind", "case"), FIDELITY_CASES, ids=[case["id"] for _, _, case in FIDELITY_CASES])
def test_real_compaction_fidelity(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, case_index: int, kind: str, case: dict[str, Any]) -> None:
    env = memory_live_api
    assert os.environ.get("MULTIRAG_MEMORY_LIVE_PROVIDER") == "OpenAI"
    require_direct_openai_models(env)
    env["journal"].event("reuse-fidelity-fixture", kind=kind, case_id=case["id"], SDK_maximum=reuse.MAX_SDK_PER_CASE)
    report = reuse.run_case(env, monkeypatch, case, case_index, Path(request.config.getoption("--test-report-dir")))
    assert report["mechanism_complete"] is True and report["withdrawal_prepare_rejected_no_model"] is True
    assert len(report["actual_attempts"]) == reuse.MAX_SDK_PER_CASE
    if kind == "fixed_retained":
        suffix = report["sources"][1]
        retained = {"source_id": suffix["source_id"], "content": suffix["content"]}
        assert "只切换预览背景，控制值没有修改" in retained["content"]
        row = next(answer for answer in report["answers"] if answer["mode"] == "compacted" and answer["reuse_index"] == 2)
        assert row["fresh_get_count"] == 1
        prepared = row["prepared"]["request"]
        assert prepared == row["actual_wire"][0]["body"]
        payload = json.loads(prepared["messages"][1]["content"])["retained"]
        assert payload == row["fresh_result"]["artifact"]["package"]["retained"] and len(payload) == 1
        assert {key: payload[0][key] for key in retained} == retained
        assert prepared["messages"][2]["content"] == case["followups"][0]["question"]
    assert report["semantic_answers_accepted"] is True


@pytest.mark.parametrize("expansion", [False, True], ids=["legacy", "expanded"])
def test_controlled_compaction_reuse_sdk(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, expansion: bool) -> None:
    """Actual API/PG/Milvus/prepared SDK, strict fake HTTP and simulated reviewers."""
    env = memory_live_api
    require_direct_openai_models(env)
    env["reuse_controlled_protocol_only"] = True
    observed: list[dict[str, Any]] = []
    original_sync, original_async = httpx.Client.send, httpx.AsyncClient.send

    def sync(client: httpx.Client, outgoing: httpx.Request, *args: Any, **options: Any) -> httpx.Response:
        if outgoing.url.host != "api.openai.com":
            return original_sync(client, outgoing, *args, **options)
        assert outgoing.method == "POST" and outgoing.url.path.endswith("/embeddings")
        body = json.loads(outgoing.content)
        assert body["model"] == "text-embedding-3-small" and len(body["input"]) == 1
        observed.append({"kind": "embedding", "body": body})
        return httpx.Response(
            200,
            request=outgoing,
            json={"object": "list", "data": [{"object": "embedding", "index": 0, "embedding": [0.25] * 1536}], "model": body["model"], "usage": {"prompt_tokens": 17, "total_tokens": 17}},
        )

    async def asynchronous(client: httpx.AsyncClient, outgoing: httpx.Request, *args: Any, **options: Any) -> httpx.Response:
        if outgoing.url.host != "api.openai.com":
            return await original_async(client, outgoing, *args, **options)
        assert outgoing.method == "POST" and outgoing.url.path.endswith("/chat/completions")
        body = json.loads(outgoing.content)
        assert set(body) == {"model", "messages", "max_completion_tokens"} and body["model"] == "gpt-6.1-terra" and body["max_completion_tokens"] == 512
        observed.append({"kind": "chat", "body": body})
        text = (
            "仅合成数据、演练内网，禁止生产/真实客户数据/公网。RIV-7F2B-2026每批48条，90秒窗口，连续两窗p95均超过280毫秒才停止回退。说明不证明执行。"
            if env["observer"].stage == "analysis"
            else "受控重复SDK协议文本，不代表真实模型、事实准确率或实际人工结论。"
        )
        return httpx.Response(
            200,
            request=outgoing,
            json={
                "id": "controlled-reuse",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 31, "completion_tokens": 11, "total_tokens": 42, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 0}},
            },
        )

    def review(directory: Path, label: str, value: dict[str, Any], journal: Any, **options: Any) -> dict[str, Any]:
        result = {
            "request_sha256": original.digest(value),
            "reviewed_by": "controlled_protocol_simulation_only",
            "synthetic_only": True,
            "judgments": {
                key: {"required": {row["id"]: "satisfied" for row in item["rubric"]["required"]}, "forbidden": {row["id"]: "satisfied" for row in item["rubric"]["forbidden"]}, "accepted": True}
                for key, item in value["review_items"].items()
            },
            "notes": "Simulated protocol only; not actual human acceptance or semantic quality.",
        }
        return original.validate_judgment(value, result)

    monkeypatch.setattr(httpx.Client, "send", sync)
    monkeypatch.setattr(httpx.AsyncClient, "send", asynchronous)
    monkeypatch.setattr(original, "external_review", review)
    case = original.load_cases(BASELINE, "known_baseline")[0]
    if expansion:
        case["id"] = "controlled_" + case["id"]
        case.update(reuse_kind="longitudinal", followups=[{"question": f"第{index}次检查应核对哪些既有约束？", "rubric": case["rubric"]["answer"]} for index in (2, 3, 4)])
    report = reuse.run_case(env, monkeypatch, case, 0, Path(request.config.getoption("--test-report-dir")))
    assert report["mechanism_complete"] is True and len(observed) == len(env["observer"].attempts) == 11
    assert len(report["answers"]) == 8 and all(row["fresh_get_count"] == 1 for row in report["answers"])
    assert report["withdrawal_prepare_rejected_no_model"] is True
    assert report["cumulative_cost"]["prefixes"]["4"]["complete_pairs"] is True
    assert sum(row["kind"] == "chat" for row in observed) == 9
    if expansion:
        assert report["semantic_prefixes"]["4"]["pair_outcomes"]["both_accepted"] == 4
        assert len({row["body"]["messages"][2]["content"] for row in observed if row["kind"] == "chat" and len(row["body"]["messages"]) == 3}) == 4
        assert report["cumulative_cost"]["prefixes"]["4"]["known_bucket_reference"]["compacted_incremental_including_one_summary"]["attempt_count"] == 5
    env["journal"].event(
        "controlled-reuse-complete", actual_external_calls=0, SDK_calls=11, provider_usage_simulated=True, review_simulated=True, summary_count=1, answer_count=8, fresh_GET_per_answer=True
    )
