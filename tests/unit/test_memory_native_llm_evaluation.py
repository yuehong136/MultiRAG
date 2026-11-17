from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from tests.support import memory_compaction_evaluation as compaction
from tests.support import memory_native_llm_evaluation as native
from tests.support.memory_workload_models import ProviderObserver


def case() -> dict[str, Any]:
    return native.load_cases(Path(__file__).parents[1] / "fixtures/memory_native_llm_baseline.json", "known_baseline")[0]


def test_fixed_case_plan_and_no_oracle_in_generation() -> None:
    value = case()
    before = deepcopy(value)
    assert native.MAX_SDK_PER_CASE * 2 == 10 and native.COMPLETION_RESERVE_PER_CASE * 2 == 5120
    assert native.validate_case(value) == before and value == before
    assert value["question"] not in native.GENERATION_SYSTEM
    assert all(row["description"] not in native.GENERATION_SYSTEM for group in value["rubric"].values() for rows in group.values() for row in rows)


def test_message_wrapping_and_RAW_keep_exact_negation_values() -> None:
    text = "不启用缓存，不能说不设置过期时间；0 false [] 仅双条件同时成立。{literal} {{data}}"
    assert native.selected_output(text, 0) == text
    rendered = native.selected_output(text, 1)
    assert rendered == native.TEMPLATE_PREFIX + text + native.TEMPLATE_SUFFIX
    raw = json.loads(native.expected_raw("原始事实", rendered))
    assert raw["segments"] == [{"role": "user", "content": "原始事实"}, {"role": "assistant", "content": rendered}]
    assert raw["event_time"] is None


def test_actual_SSE_usage_empty_choices_after_finish_and_UTF8_split() -> None:
    receipt = native.SSEReceipt()
    events: list[dict[str, Any]] = [
        {"model": "actual", "choices": [{"delta": {"content": "不启用缓存0/false/[]"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}},
        {"choices": [], "usage": {"prompt_tokens": 81, "completion_tokens": 20, "total_tokens": 101}},
    ]
    raw = ("".join("data: " + json.dumps(row, ensure_ascii=False) + "\n\n" for row in events) + "data: [DONE]\n\n").encode()
    for offset in range(0, len(raw), 7):
        receipt.feed(raw[offset : offset + 7])
    assert receipt.text == "不启用缓存0/false/[]" and receipt.finish_reason == "stop" and receipt.done
    assert receipt.usage == events[-1]["usage"] and receipt.usage_receipt_count == 2
    assert receipt.usage is not None
    assert receipt.usage["total_tokens"] == 101  # Final cumulative receipt, never 201.


def test_missing_stream_usage_stays_unknown() -> None:
    receipt = native.SSEReceipt()
    receipt.feed(b'data: {"choices":[{"delta":{"content":"0"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    assert receipt.finish_reason == "stop" and receipt.text == "0" and receipt.usage is None


@pytest.mark.parametrize("rating", ["violated", "unassessable"])
def test_ambiguous_or_inverted_negation_cannot_be_accepted(rating: str) -> None:
    requested = native.review_request(case(), "summary", {"artifact": {"summary": "否定表达存在歧义"}})
    decision: dict[str, Any] = {
        "request_sha256": compaction.digest(requested),
        "reviewed_by": "independent",
        "synthetic_only": True,
        "judgments": {
            "artifact": {
                "required": {r["id"]: "satisfied" for r in case()["rubric"]["summary"]["required"]},
                "forbidden": {r["id"]: "satisfied" for r in case()["rubric"]["summary"]["forbidden"]},
                "accepted": True,
            }
        },
        "notes": "Explicit manual judgment, no keyword score.",
    }
    decision["judgments"]["artifact"]["required"]["negation_values"] = rating
    with pytest.raises(ValueError, match="acceptance_conflicts"):
        compaction.validate_judgment(requested, decision)
    decision["judgments"]["artifact"]["accepted"] = False
    assert compaction.validate_judgment(requested, decision)["judgments"]["artifact"]["accepted"] is False


def test_failed_native_attempt_remains_in_own_cost_bucket() -> None:
    attempt: dict[str, Any] = {
        "attempt_id": "generation",
        "stage": "analysis",
        "workload_phase": "native",
        "model_reference": "actual",
        "config_reference": "fixed",
        "state": "failed",
        "provider_total_tokens": None,
        "usage": {},
        "elapsed_seconds": 1.0,
    }
    result = native.cost_partition([attempt], ["generation"])
    assert result["groups"]["shared_native_generation"]["attempt_count"] == 1
    assert result["groups"]["shared_native_generation"]["provider_total_tokens"] is None
    assert result["groups"]["summary"]["attempt_count"] == 0 and result["all_actual"]["token_price_estimate"] is None


@pytest.mark.parametrize(
    "attempts",
    [
        [],
        [{"state": "completed", "finish_reason": "length"}],
        [{"state": "completed", "finish_reason": None}],
        [{"state": "failed", "finish_reason": "stop"}],
        [{"state": "completed", "finish_reason": "stop"}] * 2,
    ],
)
def test_nonempty_generated_artifact_does_not_prove_summary_completed(attempts: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError, match="summary_not_observed_complete"):
        native.require_complete_summary(attempts)


def test_one_complete_summary_without_reported_usage_still_requires_stop() -> None:
    native.require_complete_summary([{"state": "completed", "finish_reason": "stop", "provider_total_tokens": None}])


class Journal:
    def event(self, *args: Any, **kwargs: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_second_native_transport_refused_before_any_second_send(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def send(client: httpx.AsyncClient, request: httpx.Request, *args: Any, **options: Any) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=b"data: [DONE]\n\n")

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    env = {"observer": ProviderObserver(cast(Any, Journal())), "model_ids": {"chat": 1}}
    value = case()
    body = {
        "model": "gpt-6.1-terra",
        "messages": [{"role": "system", "content": native.GENERATION_SYSTEM}, {"role": "user", "content": value["user_input"]}],
        "max_completion_tokens": 1024,
        "stream": True,
    }
    with native.native_provider_scope(env, monkeypatch, value, 0) as progress:
        async with httpx.AsyncClient() as client:
            first = await client.send(httpx.Request("POST", "https://api.openai.com/v1/chat/completions", json=body))
            assert await first.aread() == b"data: [DONE]\n\n"
            with pytest.raises(RuntimeError, match="retry_blocked"):
                await client.send(httpx.Request("POST", "https://api.openai.com/v1/chat/completions", json=body))
    assert len(calls) == 1 and progress["blocked_extra_native_requests"] == 1
