"""Fresh-consumption, masked review and exact single-trajectory accounting."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from memory.compaction.consumption import DATA_ONLY_SYSTEM
from memory.compaction.domain import input_fingerprint, package_for
from tests.support import memory_compaction_evaluation as original
from tests.support import memory_compaction_reuse_evaluation as reuse


def current(
    retained_content: str = "完整保留：限额0，待办[]，不能把0或[]当未知。",
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[original.Mode, dict[str, Any]]]:
    sources: list[dict[str, Any]] = []
    for index, content in enumerate(("第一完整原文：只能演练，禁止生产，编号KEEP-001。", retained_content)):
        identity = str(2**53 + 101 + index)
        sources.append(
            {
                "source_id": identity,
                "content": content,
                "provenance": {
                    "memory_id": "f" * 32,
                    "source_revision": "1",
                    "source_generation": "3",
                    "content_sha256": original.content_hash(content),
                    "scope": {"user_id": original.SUBJECT, "agent_id": original.AGENT, "session_id": str(index)},
                    "provenance_sha256": None,
                    "format": "legacy_combined",
                    "role_status": "role_unknown",
                    "reference_time": None,
                    "event_time": {"value": None, "status": "unknown"},
                },
            }
        )
    roots = [{"binding": original.source_binding(row), "content": row["content"], "declared_roles": []} for row in sources]
    artifact = {
        "memory_id": "f" * 32,
        "operation_id": "original-op",
        "artifact_id": "a" * 32,
        "input_manifest_sha256": input_fingerprint(roots),
        "state": "generated",
        "valid_for_current": True,
        "validity_checked": True,
        "snapshot_only": True,
        "redacted": False,
        "sources": [row["binding"] for row in roots],
        "package": package_for(roots, prefix_count=1, summary="仅演练，禁止生产；KEEP-001。"),
    }
    result = {
        "operation": {
            "memory_id": artifact["memory_id"],
            "operation_id": artifact["operation_id"],
            "artifact_id": artifact["artifact_id"],
            "kind": "context_compact",
            "status": "generated",
            "writer_busy": False,
            "needs_operator_reconciliation": False,
        },
        "artifact": artifact,
    }
    request = original.compaction_request([row["source_id"] for row in sources], "original-op")
    return sources, result, request, original.comparison_packages(sources, request, artifact)


def test_fixed_plan_and_counterbalance_do_not_rerun_prefix_subsets() -> None:
    assert reuse.REPETITIONS == 4 and reuse.PREFIXES == (1, 2, 4)
    assert 2 * reuse.MAX_SDK_PER_CASE == 22 and 2 * reuse.COMPLETION_RESERVE_PER_CASE == 9216
    for index in range(1, 5):
        assert reuse.pair_order(0, index) == reuse.pair_order(1, index)[::-1]
    assert reuse.pair_order(0, 1) == reuse.pair_order(0, 2)[::-1]
    with pytest.raises(ValueError):
        reuse.pair_order(0, True)
    expanded = [reuse.pair_order(case, index, expansion=True)[0] for case in range(reuse.EXPANSION_CASES) for index in range(1, 5)]
    assert expanded.count("uncompressed") == expanded.count("compacted") == 16
    with pytest.raises(ValueError):
        reuse.pair_order(2, 1)


def expanded_case(kind: str, label: str) -> dict[str, Any]:
    base = original.load_cases(Path(__file__).parents[1] / "fixtures/memory_compaction_baseline.json", "known_baseline")[0]
    base.update(id=label, reuse_kind=kind)
    if kind == "longitudinal":
        base["followups"] = [{"question": f"第 {index} 次怎么看原约束？", "rubric": deepcopy(base["rubric"]["answer"])} for index in (2, 3, 4)]
    return base


def test_expansion_fixtures_enforce_disjoint_question_trajectories_and_balanced_inventory(tmp_path: Path) -> None:
    cases = [expanded_case(kind, f"new_case_{index}") for index, kind in enumerate(("repeat", "longitudinal", "repeat", "longitudinal"))]
    path = tmp_path / "sealed.json"
    fixture = {"schema_version": 1, "kind": "expansion_holdout", "cases": cases}
    path.write_text(json.dumps(fixture))
    assert reuse.load_expansion_cases(path, "expansion_holdout") == cases
    with pytest.raises(ValueError, match="fixture"):
        reuse.load_expansion_cases(path, "expansion_development")
    assert len({item["question"] for item in reuse.expansion_trajectory(cases[0])[1]}) == 1
    assert len({item["question"] for item in reuse.expansion_trajectory(cases[1])[1]}) == 4
    cases[1]["followups"][0]["question"] = cases[1]["question"]
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="distinct_questions"):
        reuse.load_expansion_cases(path, "expansion_holdout")


def test_every_prepare_fetches_original_operation_and_withdrawal_stops_without_fallback() -> None:
    sources, result, request, packages = current()
    reference = reuse.reference_from(result)
    reads: list[tuple[str, str]] = []

    def fetch(mid: str, operation: str) -> dict[str, Any]:
        reads.append((mid, operation))
        return deepcopy(result)

    for _ in range(4):
        row = reuse.prepare_for_mode(reference, fetch, mode="compacted", sources=sources, compaction_request=request, expected_packages=packages, question="哪些条件？")
        assert row["fresh_get_count"] == 1 and row["prepared"]["request"]["messages"][0]["content"] == DATA_ONLY_SYSTEM
    assert reads == [(reference["memory_id"], reference["operation_id"])] * 4
    result["artifact"].update(state="stale", valid_for_current=False, package=None)
    with pytest.raises(ValueError, match="not_consumable"):
        reuse.prepare_for_mode(reference, fetch, mode="compacted", sources=sources, compaction_request=request, expected_packages=packages, question="哪些条件？")
    assert len(reads) == 5


def test_original_arm_keeps_all_roots_and_uses_same_system_and_exact_three_key_shape() -> None:
    sources, result, request, packages = current()
    row = reuse.prepare_for_mode(reuse.reference_from(result), lambda *_: result, mode="uncompressed", sources=sources, compaction_request=request, expected_packages=packages, question="条件？")
    body = row["prepared"]["request"]
    assert set(body) == {"model", "messages", "max_completion_tokens"}
    assert body["messages"][0]["content"] == DATA_ONLY_SYSTEM
    assert json.loads(body["messages"][1]["content"]) == packages["uncompressed"]
    assert all(source["content"] in body["messages"][1]["content"] for source in sources)
    assert row["prepared"]["budget"]["request_sha256"] == original.digest(body)


def test_background_followup_carries_unchanged_settings_evidence_from_retained_source() -> None:
    evidence = "本轮只切换预览背景，三个控制值没有修改。"
    sources, result, request, packages = current(evidence)
    question = "预览背景切换是否改变三个控制值？"
    row = reuse.prepare_for_mode(reuse.reference_from(result), lambda *_: result, mode="compacted", sources=sources, compaction_request=request, expected_packages=packages, question=question)
    messages = row["prepared"]["request"]["messages"]
    retained = json.loads(messages[1]["content"])["retained"]
    assert row["fresh_get_count"] == 1
    assert len(retained) == 1 and retained[0]["source_id"] == sources[1]["source_id"] and retained[0]["content"] == evidence
    assert messages[2]["content"] == question


def trajectory() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = [
        {"attempt_id": "source1", "stage": "source_embedding", "state": "completed", "provider_total_tokens": 17},
        {"attempt_id": "source2", "stage": "source_embedding", "state": "completed", "provider_total_tokens": 17},
        {"attempt_id": "summary", "stage": "analysis", "state": "completed", "provider_total_tokens": 100, "elapsed_seconds": 1},
    ]
    answers = []
    for index, (plain, compact) in enumerate(((10, 5), (20, 8), (30, 11), (40, 13)), start=1):
        for mode, tokens in (("uncompressed", plain), ("compacted", compact)):
            identity = f"{index}-{mode}"
            attempts.append(
                {
                    "attempt_id": identity,
                    "stage": "answer",
                    "state": "completed",
                    "provider_total_tokens": tokens,
                    "elapsed_seconds": 1,
                    "provider_usage_raw": {"prompt_tokens_details": {"cached_tokens": None if index == 1 else 0}},
                }
            )
            answers.append({"reuse_index": index, "mode": mode, "attempt_ids": [identity], "response": {"attempt_id": identity}})
    return attempts, answers


def test_1_2_4_costs_are_prefixes_with_shared_prep_once_and_summary_once() -> None:
    attempts, answers = trajectory()
    before = deepcopy((attempts, answers))
    result = reuse.cumulative_cost(attempts, answers)
    assert result["shared_source_preparation_counted_once"]["provider_total_tokens"] == 34
    assert result["prefixes"]["1"]["uncompressed_incremental"]["provider_total_tokens"] == 10
    assert result["prefixes"]["2"]["uncompressed_incremental"]["provider_total_tokens"] == 30
    assert result["prefixes"]["4"]["uncompressed_incremental"]["provider_total_tokens"] == 100
    assert [result["prefixes"][str(i)]["compacted_incremental_including_one_summary"]["provider_total_tokens"] for i in (1, 2, 4)] == [105, 113, 137]
    assert result["all_actual_attempts"]["provider_total_tokens"] == 271
    assert result["prefixes"]["4"]["sdk_elapsed_seconds"]["compacted_including_summary"] == 5
    assert before == (attempts, answers) and result["all_actual_attempts"]["token_price_estimate"] is None
    assert result["prefixes"]["1"]["observed_cache"]["compacted"]["known_cached_tokens_subtotal"] is None
    assert result["prefixes"]["2"]["observed_cache"]["compacted"]["unreported_cache_call_count"] == 1


def test_failed_answer_remains_in_its_arm_prefix_unknown_cost() -> None:
    attempts, answers = trajectory()
    failed = next(row for row in attempts if row["attempt_id"] == "2-compacted")
    failed.update(state="failed", provider_total_tokens=None)
    record = next(row for row in answers if row["reuse_index"] == 2 and row["mode"] == "compacted")
    record.pop("response")
    result = reuse.cumulative_cost(attempts, answers)
    arm = result["prefixes"]["2"]["compacted_incremental_including_one_summary"]
    assert arm["attempt_count"] == 3 and arm["provider_total_tokens"] is None and arm["known_provider_total_tokens_subtotal"] == 105
    assert result["prefixes"]["2"]["complete_pairs"] is False
    assert result["all_actual_attempts"]["provider_total_tokens"] is None


def test_cumulative_cache_price_subtotals_do_not_infer_cache_write_or_invoices() -> None:
    attempts, answers = trajectory()
    by_id = {row["attempt_id"]: row for row in attempts}
    quote = {
        "terra": {
            "source": "provider-model-page",
            "as_of": "2026-11-22",
            "currency": "USD",
            "per_million": {"input_tokens": "1", "output_tokens": "5", "cache_read_input_tokens": ".05"},
        }
    }
    for key, (ordinary, cached, output) in {"summary": (200, 40, 20), "1-uncompressed": (100, 800, 10), "1-compacted": (100, 0, 10)}.items():
        by_id[key].update(model_reference="terra", usage={"input_tokens": ordinary, "cache_read_input_tokens": cached, "output_tokens": output}, provider_total_tokens=ordinary + cached + output)
    cost = reuse.cumulative_cost(attempts, answers, prices=quote, include_known_buckets=True)["prefixes"]["1"]
    known = cost["known_bucket_reference"]
    assert known["uncompressed_incremental"]["known_bucket_subtotal"] == "0.00019000"
    assert known["compacted_incremental_including_one_summary"]["known_bucket_subtotal"] == "0.00045200"
    assert known["uncompressed_incremental"]["by_bucket"]["cache_read_input_tokens"]["known_token_subtotal"] == 800
    assert known["compacted_incremental_including_one_summary"]["attempts"][0]["excluded_buckets"]["cache_creation_input_tokens"] is None
    assert cost["compacted_incremental_including_one_summary"]["token_price_estimate"] is None


def test_expanded_review_masks_arms_and_keeps_missing_and_rejected_pairs_distinct() -> None:
    _, answers = trajectory()
    sources, _, _, _ = current()
    case, prompts = reuse.expansion_trajectory(expanded_case("longitudinal", "review_case"))
    for row in answers:
        row["response"]["answer"] = "same visible answer"
        row["prepared"] = {"request": "private arm-specific context"}
    packet, mapping = reuse.blind_answer_review(case["id"], sources, answers, prompts)
    assert set(packet["review_items"]) == set(mapping)
    assert list(packet["review_items"]) == sorted(mapping)
    assert all(len(key) == 32 and key.isalnum() for key in mapping)
    assert set(packet["review_items"][next(iter(mapping))]) == {"question", "rubric", "answer"}
    assert all(set(source) == {"source_id", "content"} for source in packet["sources"])
    assert "private arm-specific context" not in json.dumps(packet)
    judgments = {
        key: {"accepted": item["mode"] == "uncompressed", "required": {"rule": "satisfied"}, "forbidden": {"boundary": "unassessable"}} for key, item in mapping.items() if item["reuse_index"] == 1
    }
    quality = reuse.semantic_prefixes(mapping, judgments)
    assert quality["1"]["complete_pairs"] and quality["1"]["pair_outcomes"]["raw_only_accepted"] == 1
    assert quality["4"]["expected_pairs"] == 4 and quality["4"]["evaluated_pairs"] == 1 and not quality["4"]["complete_pairs"]
    assert quality["1"]["by_mode"]["compacted"]["criteria"]["forbidden"]["unassessable"] == 1


def test_duplicate_attempt_cannot_be_charged_as_two_answers() -> None:
    attempts, answers = trajectory()
    answers[1]["attempt_ids"] = answers[0]["attempt_ids"]
    with pytest.raises(ValueError, match="duplicate"):
        reuse.cumulative_cost(attempts, answers)


async def test_full_sdk_body_must_equal_shared_builder_not_just_messages() -> None:
    sources, result, request, packages = current()
    prepared = reuse.prepare_for_mode(reuse.reference_from(result), lambda *_: result, mode="compacted", sources=sources, compaction_request=request, expected_packages=packages, question="条件？")[
        "prepared"
    ]
    delivered: list[dict[str, Any]] = []

    async def send(outgoing: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(outgoing.content))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with reuse.exact_wire_guard(prepared) as records:
            await client.post("https://api.openai.com/v1/chat/completions", json=prepared["request"])
    assert delivered == [prepared["request"]] and len(records) == 1
    changed = {**deepcopy(prepared["request"]), "stream": False}
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with reuse.exact_wire_guard(prepared), pytest.raises(AssertionError, match="differs"):
            await client.post("https://api.openai.com/v1/chat/completions", json=changed)
    assert len(delivered) == 1
