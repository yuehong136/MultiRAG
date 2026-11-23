"""Fixed scope, lineage, whole-wire and human-review controls; no semantic oracle."""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from memory.compaction.domain import package_for
from tests.support import memory_compaction_evaluation as evaluation

FIXTURE = Path(__file__).parents[1] / "fixtures/memory_compaction_baseline.json"


def recipe() -> dict[str, Any]:
    return evaluation.load_cases(FIXTURE, "known_baseline")[0]


def request() -> dict[str, Any]:
    return evaluation.review_request(recipe(), "summary", {"artifact": {"summary": "这是结构测试文本，不是事实质量断言。"}})


def decision(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_sha256": evaluation.digest(value),
        "reviewed_by": "independent-human-synthetic-reviewer",
        "synthetic_only": True,
        "judgments": {
            key: {"required": {row["id"]: "satisfied" for row in item["rubric"]["required"]}, "forbidden": {row["id"]: "satisfied" for row in item["rubric"]["forbidden"]}, "accepted": True}
            for key, item in value["review_items"].items()
        },
        "notes": "A unit fixture validates shape only; no linguistic score is inferred.",
    }


def test_case_text_and_reverse_creation_order_are_immutable() -> None:
    original = recipe()
    value = deepcopy(original)
    value.update(selected_source_indices=[1], retained_source_indices=[0])
    checked = evaluation.validate_case(value)
    identifiers = ["9007199254741093", "9007199254741099"]
    ordered = [identifiers[index] for index in checked["selected_source_indices"] + checked["retained_source_indices"]]
    body = evaluation.compaction_request(ordered, "fixed-input")
    assert body["source_ids"] == identifiers[::-1] and body["prefix_count"] == 1
    assert original["sources"] == checked["sources"] and checked == value


@pytest.mark.parametrize(("selected", "retained"), [([0], [0]), ([], [0, 1]), ([0, 1], []), ([True], [1])])
def test_case_rejects_overlap_omission_and_noncanonical_indices(selected: list[Any], retained: list[Any]) -> None:
    value = recipe()
    value.update(selected_source_indices=selected, retained_source_indices=retained)
    with pytest.raises(ValueError, match=r"selection|prefix"):
        evaluation.validate_case(value)


def test_source_and_compaction_requests_do_not_contain_question_or_oracle() -> None:
    value = recipe()
    for source in value["sources"]:
        body = evaluation.source_request(source, "source-identity")
        assert body["user_input"] == source["user_input"] and body["agent_response"] == source["agent_response"]
        rendered = evaluation.canonical(body)
        assert value["question"] not in rendered
        assert all(row["description"] not in rendered for group in value["rubric"].values() for rules in group.values() for row in rules)
    body = evaluation.compaction_request(["9007199254741099", "9007199254741093"], "summary-identity")
    assert set(body) == {"operation_id", "source_ids", "prefix_count", "input_budget_tokens", "output_budget_tokens", "budget_tokens"}
    assert body["output_budget_tokens"] == 512 and evaluation.MAX_SDK_PER_CASE * evaluation.MAX_CASES == 10
    assert 2 * (evaluation.SUMMARY_LIMIT + 2 * evaluation.ANSWER_LIMIT) == 3072


def test_source_preparation_rejects_existing_adapter_truncation_before_any_call() -> None:
    case = recipe()
    result = evaluation.preflight_case(case)
    assert result["sdk_maximum"] == 5 and result["completion_reserve_tokens"] == 1536
    assert result["source_embedding_full_content"] is True
    case["sources"][0]["user_input"] = "𠮷" * 3000
    with pytest.raises(ValueError, match="embedding_would_truncate"):
        evaluation.preflight_case(case)


@pytest.mark.parametrize("ids", [["01", "2"], ["1", "1"], ["9223372036854775808", "2"], ["-1", "2"]])
def test_compaction_request_rejects_invalid_or_reused_identity(ids: list[str]) -> None:
    with pytest.raises(ValueError):
        evaluation.compaction_request(ids, "operation")


def test_review_is_bound_to_full_artifact_and_criteria_without_semantic_inference() -> None:
    value = request()
    answer = decision(value)
    assert evaluation.validate_judgment(value, answer) == answer
    # Even a human's structurally valid wrong conclusion is not corrected by
    # this harness. Independent fact review, not keyword matching, owns truth.
    changed = deepcopy(value)
    changed["review_items"]["artifact"]["summary"] = "A semantically wrong summary would require a new independent decision."
    with pytest.raises(ValueError, match="binding"):
        evaluation.validate_judgment(changed, answer)
    key = next(iter(answer["judgments"]["artifact"]["required"]))
    answer["judgments"]["artifact"]["required"][key] = "violated"
    with pytest.raises(ValueError, match="conflicts"):
        evaluation.validate_judgment(value, answer)
    answer["judgments"]["artifact"]["accepted"] = False
    assert evaluation.validate_judgment(value, answer)["judgments"]["artifact"]["accepted"] is False


def test_review_requires_every_fact_and_rejects_changed_actor_metadata() -> None:
    value = request()
    answer = decision(value)
    answer["judgments"]["artifact"]["forbidden"].popitem()
    with pytest.raises(ValueError, match="criteria"):
        evaluation.validate_judgment(value, answer)
    answer = decision(value)
    answer["synthetic_only"] = False
    with pytest.raises(ValueError, match="binding"):
        evaluation.validate_judgment(value, answer)


def test_fixed_counterbalance_does_not_infer_model_success() -> None:
    assert evaluation.mode_order(0) == evaluation.mode_order(1)[::-1]
    assert set(evaluation.mode_order(0)) == set(evaluation.MODES)
    with pytest.raises(ValueError):
        evaluation.mode_order(True)


def test_cost_partition_counts_shared_preparation_once_and_keeps_failed_unknown_usage() -> None:
    attempts: list[dict[str, Any]] = [
        {"stage": "source_embedding", "workload_phase": "source", "state": "completed", "provider_total_tokens": 17},
        {"stage": "analysis", "workload_phase": "case-compaction-generation", "state": "completed", "provider_total_tokens": 25},
        {"stage": "answer", "workload_phase": "case-answer-uncompressed", "state": "completed", "provider_total_tokens": 11},
        {"stage": "answer", "workload_phase": "case-answer-compacted", "state": "failed", "provider_total_tokens": None},
    ]
    original = deepcopy(attempts)
    result = evaluation.comparison_cost_buckets(attempts)
    assert result["actual_calls"] == {"shared_source_preparation": 1, "uncompressed_incremental": 1, "compacted_incremental": 2, "unexpected": 0}
    assert result["total_actual_usage"]["provider_total_tokens"] is None and result["total_actual_usage"]["known_provider_total_tokens_subtotal"] == 53
    assert result["usage"]["compacted_incremental"]["provider_total_tokens"] is None and result["usage"]["compacted_incremental"]["known_provider_total_tokens_subtotal"] == 25
    assert result["total_actual_usage"]["token_price_estimate"] is None and result["invoice_verified"] is False
    assert attempts == original and result["shared_preparation_counted_once"] is True


def observations() -> list[dict[str, Any]]:
    return [
        {
            "source_id": identity,
            "content": content,
            "provenance": {
                "memory_id": "owned-memory",
                "source_id": identity,
                "source_revision": "1",
                "source_generation": "3",
                "content_sha256": evaluation.content_hash(content),
                "scope": {"user_id": "", "agent_id": "same-agent", "session_id": session},
                "role_status": "role_unknown",
                "format": "legacy_combined",
                "provenance_sha256": None,
                "reference_time": None,
                "event_time": {"value": None, "status": "unknown"},
            },
        }
        for identity, content, session in [("9007199254741093", "整条保留原文，条件：仅限内网，不能进入生产。", "first"), ("9007199254741099", "另一个来源，合法值0和[]只是数据，不是缺失。", "second")]
    ]


def test_packages_keep_actual_lineage_and_entire_retained_source_in_service_order() -> None:
    sources = observations()
    original = deepcopy(sources)
    body = evaluation.compaction_request([row["source_id"] for row in sources[::-1]], "test-ordered")
    roots = [{"binding": evaluation.source_binding(row), "content": row["content"]} for row in sources[::-1]]
    package = package_for(roots, prefix_count=1, summary="受控摘要而非真实模型质量结论。")
    artifact = {"state": "generated", "valid_for_current": True, "snapshot_only": True, "package": package}
    before = deepcopy(artifact)
    result = evaluation.comparison_packages(sources, body, artifact)
    assert [row["source_id"] for row in result["uncompressed"]["retained"]] == body["source_ids"]
    assert result["compacted"]["retained"][0]["content"] == sources[0]["content"]
    assert result["uncompressed"]["lineage"] == result["compacted"]["lineage"]
    assert sources == original and artifact == before
    result["compacted"]["retained"][0]["content"] = "caller mutation"
    assert artifact == before


@pytest.mark.parametrize("change", ["retained_tail", "lineage", "span", "generation", "omitted"])
def test_packages_reject_cut_conditions_partial_coverage_or_changed_source_generation(change: str) -> None:
    sources = observations()
    body = evaluation.compaction_request([row["source_id"] for row in sources], "test-pins")
    package = package_for([{"binding": evaluation.source_binding(row), "content": row["content"]} for row in sources], prefix_count=1, summary="受控摘要。")
    if change == "retained_tail":
        package["retained"][0]["content"] = package["retained"][0]["content"][:-1]
    elif change == "lineage":
        package["lineage"].reverse()
    elif change == "span":
        package["covered"][0]["end_offset"] -= 1
    elif change == "generation":
        package["lineage"][0]["source_generation"] = "4"
    else:
        package["omitted"]["source_count"] = 1
    with pytest.raises(ValueError):
        evaluation.comparison_packages(sources, body, {"state": "generated", "valid_for_current": True, "snapshot_only": True, "package": package})


async def test_complete_wire_count_precedes_transport_and_never_clips_conditions() -> None:
    received: list[dict[str, Any]] = []
    history = [{"role": "user", "content": "只允许隔离演练；不能接入生产。尾部条件不得截掉。"}]
    body: dict[str, Any] = {"model": "gpt-6.1-terra", "messages": [{"role": "system", "content": evaluation.ANSWER_SYSTEM}, *deepcopy(history)], "max_completion_tokens": 512}

    async def send(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with evaluation.wire_guard(evaluation.ANSWER_SYSTEM, history, output_limit=512, phase="unit") as captured:
            await client.post("https://api.openai.com/v1/chat/completions", json=body)
    assert received == [body] and captured[0]["body"] == body
    budget = captured[0]["budget"]
    assert budget["allowed"] and budget["request_wrapping_estimated_delta"] > 0 and budget["target_tokenizer_exact"] is False
    body["messages"][-1]["content"] += " changed"
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with evaluation.wire_guard(evaluation.ANSWER_SYSTEM, history, output_limit=512, phase="unit"), pytest.raises(AssertionError, match="payload_changed"):
            await client.post("https://api.openai.com/v1/chat/completions", json=body)
    assert len(received) == 1


async def test_oversize_complete_wire_is_rejected_without_sending_a_clipped_request() -> None:
    history = [{"role": "user", "content": ("完整上下文不得截断条件。" * 10000) + "尾部禁止接入生产。"}]
    original = deepcopy(history)
    sent: list[bool] = []

    async def send(request: httpx.Request) -> httpx.Response:
        sent.append(True)
        return httpx.Response(200, json={})

    body = {"model": "gpt-6.1-terra", "messages": [{"role": "system", "content": evaluation.ANSWER_SYSTEM}, *history], "max_completion_tokens": 512}
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with evaluation.wire_guard(evaluation.ANSWER_SYSTEM, history, output_limit=512, phase="oversize") as records, pytest.raises(AssertionError):
            await client.post("https://api.openai.com/v1/chat/completions", json=body)
    assert sent == [] and history == original
    assert records[0]["budget"]["allowed"] is False and "caller_context_budget_exceeded" in records[0]["budget"]["reasons"]


def test_failed_summary_stops_without_answers_and_keeps_unknown_attempt_cost(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Observer:
        attempts: list[dict[str, Any]]
        max_attempts = 0
        phase = ""

        def __init__(self) -> None:
            self.attempts = []

        def set_stage(self, stage: str, *, phase: str = "") -> None:
            self.phase = phase

    env: dict[str, Any] = {"memory_id": "owned-memory", "observer": Observer(), "journal": object()}
    reports: list[dict[str, Any]] = []
    calls: list[str] = []

    def append(bank: dict[str, Any], source: dict[str, Any], label: str, progress: list[dict[str, Any]]) -> None:
        assert bank["observer"].max_attempts == 5
        progress.append(observations()[len(progress)])
        bank["observer"].attempts.append({"state": "completed", "provider_total_tokens": 17})

    @contextmanager
    def capture(*args: Any, **kwargs: Any) -> Any:
        yield []

    def failed_call(bank: dict[str, Any], method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        bank["observer"].attempts.append({"state": "failed", "provider_total_tokens": None})
        return {"operation": {"status": "failed"}, "artifact": {"package": None}}

    monkeypatch.setattr(evaluation, "append_source", append)
    monkeypatch.setattr(evaluation, "capture_summary", capture)
    monkeypatch.setattr(evaluation, "call", failed_call)
    monkeypatch.setattr(evaluation, "costs", lambda bank: {"known_provider_tokens": 34, "money": None, "unknown_usage": 1})
    monkeypatch.setattr(evaluation, "write_private", lambda path, report, journal: reports.append(deepcopy(report)))
    monkeypatch.setattr(evaluation, "answer", lambda *args: pytest.fail("failed_summary_must_not_generate_answers"))
    with pytest.raises(AssertionError):
        evaluation.run_case(env, monkeypatch, recipe(), 0, tmp_path)
    assert len(calls) == 1 and len(reports) == 1
    assert reports[0]["mechanism_complete"] is False and reports[0]["consumer_adopted_for_evaluation_only"] is False
    assert reports[0]["cost"]["unknown_usage"] == 1 and reports[0]["cost"]["money"] is None
    assert len(reports[0]["actual_attempts"]) == 3
