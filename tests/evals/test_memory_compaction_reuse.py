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


@pytest.mark.parametrize(("case_index", "kind", "case"), [(index, kind, case) for index, (kind, case) in enumerate(CASES)], ids=[case["id"] for _, case in CASES])
def test_real_compaction_reuse(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, case_index: int, kind: str, case: dict[str, Any]) -> None:
    env = memory_live_api
    assert os.environ.get("MULTIRAG_MEMORY_LIVE_PROVIDER") == "OpenAI"
    require_direct_openai_models(env)
    env["journal"].event("reuse-fixture-kind", kind=kind, case_id=case["id"], one_summary=True, same_question_repeats=4, SDK_maximum=11)
    report = reuse.run_case(env, monkeypatch, case, case_index, Path(request.config.getoption("--test-report-dir")))
    assert report["mechanism_complete"] is True


def test_controlled_compaction_reuse_sdk(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
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
    report = reuse.run_case(env, monkeypatch, case, 0, Path(request.config.getoption("--test-report-dir")))
    assert report["mechanism_complete"] is True and len(observed) == len(env["observer"].attempts) == 11
    assert len(report["answers"]) == 8 and all(row["fresh_get_count"] == 1 for row in report["answers"])
    assert report["withdrawal_prepare_rejected_no_model"] is True
    assert report["cumulative_cost"]["prefixes"]["4"]["complete_pairs"] is True
    assert sum(row["kind"] == "chat" for row in observed) == 9
    env["journal"].event(
        "controlled-reuse-complete", actual_external_calls=0, SDK_calls=11, provider_usage_simulated=True, review_simulated=True, summary_count=1, answer_count=8, fresh_GET_per_answer=True
    )
