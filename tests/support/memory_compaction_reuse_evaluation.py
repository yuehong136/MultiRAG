"""A single four-pair trajectory, fresh artifact reads, and explicit human review."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from api.db.services.llm_service import LLMBundle, llm_bundle_scope
from common.asyncio_utils import finish_owned_work
from common.constants import LLMType
from common.token_utils import encoder
from memory.compaction.consumption import DATA_ONLY_SYSTEM, prepare_compaction_request
from memory.maintenance.evaluation import usage_cost
from memory.writes.providers import MemoryWriteProviders
from tests.support import memory_compaction_evaluation as original
from tests.support.memory_startup_evaluation_budget import inspect_answer_request
from tests.support.memory_startup_evaluation_cost import known_bucket_cost
from tests.support.memory_startup_longitudinal import costs
from tests.support.memory_workload_review import call, write_private
from tests.support.memory_writes_http import api_request

REPETITIONS = 4
PREFIXES = (1, 2, 4)
MAX_SDK_PER_CASE = 11
COMPLETION_RESERVE_PER_CASE = 9 * original.ANSWER_LIMIT
EXPANSION_CASES_PER_FIXTURE = 4
EXPANSION_CASES = 2 * EXPANSION_CASES_PER_FIXTURE
FetchOperation = Callable[[str, str], dict[str, Any]]


def pair_order(case_index: int, reuse_index: int, *, expansion: bool = False) -> tuple[original.Mode, original.Mode]:
    case_count = EXPANSION_CASES if expansion else original.MAX_CASES
    if type(case_index) is not int or not 0 <= case_index < case_count or type(reuse_index) is not int or not 1 <= reuse_index <= REPETITIONS:
        raise ValueError("invalid_reuse_trajectory_index")
    return original.mode_order((case_index + reuse_index - 1) % 2)


def expansion_trajectory(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate a frozen extension without changing the legacy single-question recipe."""
    if not isinstance(value, dict) or value.get("reuse_kind") not in ("repeat", "longitudinal"):
        raise ValueError("invalid_compaction_reuse_kind")
    case = original.validate_case({key: item for key, item in value.items() if key not in {"reuse_kind", "followups"}})
    if case["selected_source_indices"] != [0] or case["retained_source_indices"] != [1]:
        raise ValueError("compaction_expansion_requires_ordered_fixture_sources")
    first = {"question": case["question"], "rubric": case["rubric"]["answer"]}
    if value["reuse_kind"] == "repeat":
        if "followups" in value:
            raise ValueError("compaction_repeat_cannot_override_followups")
        return case, [deepcopy(first) for _ in range(REPETITIONS)]
    followups = value.get("followups")
    if not isinstance(followups, list) or len(followups) != REPETITIONS - 1:
        raise ValueError("compaction_longitudinal_requires_three_followups")
    for item in followups:
        if not isinstance(item, dict) or set(item) != {"question", "rubric"}:
            raise ValueError("invalid_compaction_followup")
        original.validate_case({**case, "question": item["question"], "rubric": {"summary": case["rubric"]["summary"], "answer": item["rubric"]}})
    trajectory = [first, *deepcopy(followups)]
    if len({item["question"].strip() for item in trajectory}) != REPETITIONS:
        raise ValueError("compaction_longitudinal_requires_distinct_questions")
    return case, trajectory


def load_expansion_cases(path: Path, kind: str) -> list[dict[str, Any]]:
    fixture = json.loads(path.read_text())
    if (
        not isinstance(fixture, dict)
        or set(fixture) != {"schema_version", "kind", "cases"}
        or type(fixture["schema_version"]) is not int
        or fixture["schema_version"] != 1
        or kind not in {"expansion_development", "expansion_holdout"}
        or fixture["kind"] != kind
    ):
        raise ValueError("invalid_compaction_expansion_fixture")
    cases = fixture["cases"]
    if not isinstance(cases, list) or len(cases) != EXPANSION_CASES_PER_FIXTURE:
        raise ValueError("compaction_expansion_fixture_requires_four_cases")
    for case in cases:
        expansion_trajectory(case)
    if len({case["id"] for case in cases}) != len(cases) or sum(case["reuse_kind"] == "repeat" for case in cases) != len(cases) // 2:
        raise ValueError("compaction_expansion_fixture_requires_unique_and_balanced_cases")
    return cases


def reference_from(result: dict[str, Any]) -> dict[str, str]:
    artifact = result["artifact"]
    return {key: artifact[key] for key in ("memory_id", "operation_id", "artifact_id", "input_manifest_sha256")}


def baseline_request(reference: dict[str, str], package: dict[str, Any], question: str) -> dict[str, Any]:
    """Same consumer system and whole-wire shape, with the complete original roots."""
    request = {
        "model": "gpt-6.1-terra",
        "messages": [{"role": "system", "content": DATA_ONLY_SYSTEM}, {"role": "user", "content": original.canonical(package)}, {"role": "user", "content": question}],
        "max_completion_tokens": original.ANSWER_LIMIT,
    }
    budget = inspect_answer_request(
        request,
        mode="baseline",
        context_window_tokens=original.CALLER_LIMIT,
        output_reserve_tokens=original.ANSWER_LIMIT,
        safety_margin_tokens=original.MARGIN,
        count_tokens=lambda text: len(encoder.encode(text, disallowed_special=())),
        tokenizer_label="cl100k_base_estimate",
        components={"system": [0], "question": [2], "history": [1], "startup": [], "retrieval": []},
    )
    if not budget["allowed"]:
        raise ValueError("reuse_baseline_request_budget_exceeded")
    return {
        "reference": deepcopy(reference),
        "request": request,
        "budget": {
            "estimated_request_tokens": budget["request_estimated_tokens"],
            "output_reserve_tokens": original.ANSWER_LIMIT,
            "safety_margin_tokens": original.MARGIN,
            "required_tokens": budget["required_estimated_tokens"],
            "context_window_tokens": original.CALLER_LIMIT,
            "tokenizer_label": "cl100k_base_estimate",
            "token_count_is_estimate": True,
            "request_sha256": budget["request_sha256"],
        },
        "snapshot_only": True,
        "baseline_original_roots_not_a_generated_artifact": True,
    }


def prepare_for_mode(
    reference: dict[str, str],
    fetch_operation: FetchOperation,
    *,
    mode: original.Mode,
    sources: list[dict[str, Any]],
    compaction_request: dict[str, Any],
    expected_packages: dict[original.Mode, dict[str, Any]],
    question: str,
) -> dict[str, Any]:
    """Every invocation fetches once, including baseline freshness validation."""
    if mode not in original.MODES:
        raise ValueError("invalid_reuse_mode")
    observed: list[dict[str, Any]] = []

    def fetch(memory_id: str, operation_id: str) -> dict[str, Any]:
        result = fetch_operation(memory_id, operation_id)
        observed.append(deepcopy(result))
        return result

    prepared = prepare_compaction_request(
        reference,
        fetch,
        scope={"user_id": original.SUBJECT, "agent_id": original.AGENT, "session_id": None},
        model="gpt-6.1-terra",
        question=question,
        context_window_tokens=original.CALLER_LIMIT,
        output_reserve_tokens=original.ANSWER_LIMIT,
        safety_margin_tokens=original.MARGIN,
        count_tokens=lambda text: len(encoder.encode(text, disallowed_special=())),
        tokenizer_label="cl100k_base_estimate",
    )
    assert len(observed) == 1, "reuse_must_get_original_operation_once_per_consume"
    packages = original.comparison_packages(sources, compaction_request, observed[0]["artifact"])
    if packages != expected_packages:
        raise ValueError("reuse_artifact_changed_between_consumptions")
    if mode == "uncompressed":
        prepared = baseline_request(reference, packages[mode], question)
    if prepared["request"]["messages"][1]["content"] != original.canonical(packages[mode]):
        raise ValueError("reuse_consumption_is_not_complete")
    return {"prepared": prepared, "fresh_result": observed[0], "fresh_get_count": len(observed), "mode": mode}


@contextmanager
def exact_wire_guard(prepared: dict[str, Any]) -> Iterator[list[dict[str, Any]]]:
    """Check the whole serialized SDK body against the shared pure builder."""
    expected = deepcopy(prepared["request"])
    expected_hash = prepared["budget"]["request_sha256"]
    if original.digest(expected) != expected_hash:
        raise ValueError("reuse_request_changed_after_budget")
    original_send = httpx.AsyncClient.send
    observed: list[dict[str, Any]] = []

    async def inspect(client: httpx.AsyncClient, request: httpx.Request, *args: Any, **options: Any) -> Any:
        assert not args
        if request.method != "POST" or not request.url.path.rstrip("/").endswith("/chat/completions"):
            return await original_send(client, request, **options)
        assert request.url.host == "api.openai.com"
        body = json.loads(request.content)
        assert body == expected and original.digest(body) == expected_hash, "reuse_final_sdk_request_differs_from_pure_builder"
        assert set(body) == {"model", "messages", "max_completion_tokens"} and body["max_completion_tokens"] == 512
        observed.append({"body": body, "request_sha256": original.digest(body), "budget": deepcopy(prepared["budget"])})
        return await original_send(client, request, **options)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx.AsyncClient, "send", inspect)
        yield observed


async def send_request(env: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    prepared = record["prepared"]
    request = prepared["request"]
    observer = env["observer"]
    start = len(observer.attempts)
    engine = create_async_engine(env["engine"].url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as db:
            providers = MemoryWriteProviders(db, tenant_id=env["tenant_id"], actor_id=env["owners"][0])
            _, configuration = await providers._prepared(env["memory_id"], LLMType.CHAT.value)
            await db.rollback()
            with exact_wire_guard(prepared) as wire:
                record["actual_wire"] = wire
                async with llm_bundle_scope():
                    bundle = await finish_owned_work(asyncio.to_thread(LLMBundle.from_prepared, configuration, max_retries=0))
                    for name in ("client", "async_client"):
                        client = getattr(bundle.mdl, name, None)
                        if isinstance(client, (OpenAI, AsyncOpenAI)):
                            client.max_retries = 0
                    text = await finish_owned_work(
                        bundle.async_chat(
                            request["messages"][0]["content"], request["messages"][1:], {"max_completion_tokens": request["max_completion_tokens"]}, max_tokens=request["max_completion_tokens"]
                        )
                    )
        rows = observer.attempts[start:]
        assert len(wire) == len(rows) == 1 and rows[0]["state"] == "completed"
        assert isinstance(text, str) and text.strip() and not text.startswith("**ERROR**") and rows[0].get("finish_reason") == "stop"
        prompt = rows[0].get("provider_prompt_tokens")
        if type(prompt) is int:
            assert prompt + request["max_completion_tokens"] + original.MARGIN <= original.CALLER_LIMIT
        return {
            "answer": text,
            "attempt_id": rows[0]["attempt_id"],
            "request_sha256": prepared["budget"]["request_sha256"],
            "package_sha256": hashlib.sha256(request["messages"][1]["content"].encode()).hexdigest(),
        }
    finally:
        record["attempt_ids"] = [row["attempt_id"] for row in observer.attempts[start:]]
        await engine.dispose()


def cache_observations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = []
    for row in rows:
        raw = row.get("provider_usage_raw") or {}
        detail = raw.get("prompt_tokens_details") or {}
        value = detail.get("cached_tokens") if isinstance(detail, dict) else None
        values.append(value if type(value) is int and value >= 0 else None)
    known = [value for value in values if value is not None]
    return {
        "provider_reported_cached_tokens_by_call": values,
        "known_cached_tokens_subtotal": sum(known) if known else None,
        "unreported_cache_call_count": len(values) - len(known),
        "cache_inferred_from_latency": False,
        "money_or_discount_inferred": False,
    }


def cumulative_cost(
    attempts: list[dict[str, Any]],
    answers: list[dict[str, Any]],
    *,
    summary_elapsed_seconds: float | None = None,
    prices: dict[str, Any] | None = None,
    include_known_buckets: bool = False,
) -> dict[str, Any]:
    """Read 1/2/4 prefixes of one trajectory, charging summary exactly once."""
    by_id = {row["attempt_id"]: row for row in attempts}
    if len(by_id) != len(attempts):
        raise ValueError("duplicate_reuse_attempt")
    pairs = [(row["reuse_index"], row["mode"]) for row in answers]
    if len(set(pairs)) != len(pairs) or any(type(index) is not int or not 1 <= index <= REPETITIONS or mode not in original.MODES for index, mode in pairs):
        raise ValueError("invalid_or_duplicate_reuse_answer_slot")
    used = [identity for row in answers for identity in row.get("attempt_ids", [row["response"]["attempt_id"]] if "response" in row else [])]
    if len(set(used)) != len(used) or any(identity not in by_id or by_id[identity]["stage"] != "answer" for identity in used):
        raise ValueError("duplicate_or_missing_reuse_answer_attempt")
    source = [row for row in attempts if row["stage"] == "source_embedding"]
    summary = [row for row in attempts if row["stage"] == "analysis"]
    results: dict[str, Any] = {}

    def elapsed(rows: list[dict[str, Any]], key: str) -> float | None:
        values: list[float] = []
        for row in rows:
            value = row.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None
            number = float(value)
            if not math.isfinite(number) or number < 0:
                return None
            values.append(number)
        return sum(values) if values else None

    for count in PREFIXES:
        groups = {
            mode: [
                by_id[identity]
                for row in answers
                if row["reuse_index"] <= count and row["mode"] == mode
                for identity in row.get("attempt_ids", [row["response"]["attempt_id"]] if "response" in row else [])
            ]
            for mode in original.MODES
        }
        complete = all(len([row for row in answers if row["reuse_index"] <= count and row["mode"] == mode and "response" in row]) == count for mode in original.MODES)
        wall: dict[str, float | None] = {}
        for mode in original.MODES:
            selected = [row for row in answers if row["reuse_index"] <= count and row["mode"] == mode]
            read_time, send_time = elapsed(selected, "fresh_read_seconds"), elapsed(selected, "send_elapsed_seconds")
            wall[mode] = read_time + send_time if read_time is not None and send_time is not None else None
        if wall["compacted"] is not None:
            wall["compacted"] = wall["compacted"] + summary_elapsed_seconds if summary_elapsed_seconds is not None else None
        billed = {
            "uncompressed_incremental": groups["uncompressed"],
            "compacted_answers_only": groups["compacted"],
            "compacted_incremental_including_one_summary": summary + groups["compacted"],
        }
        results[str(count)] = {
            "prefix_of_single_trajectory": True,
            "complete_pairs": complete,
            **{key: usage_cost(rows, prices) for key, rows in billed.items()},
            "sdk_elapsed_seconds": {"uncompressed": elapsed(groups["uncompressed"], "elapsed_seconds"), "compacted_including_summary": elapsed(summary + groups["compacted"], "elapsed_seconds")},
            "consumer_wall_seconds_including_fresh_GET_and_send": wall,
            "observed_cache": {mode: cache_observations(groups[mode]) for mode in original.MODES},
        }
        if include_known_buckets:
            results[str(count)]["known_bucket_reference"] = {key: known_bucket_cost(rows, prices) for key, rows in billed.items()}
    unattributed = [row for row in attempts if row["stage"] == "answer" and row["attempt_id"] not in used]
    total = {
        "shared_source_preparation_counted_once": source,
        "summary_generation_actual": summary,
        "all_actual_attempts": attempts,
        "unattributed_answer_attempts": unattributed,
    }
    result: dict[str, Any] = {
        **{key: usage_cost(rows, prices) for key, rows in total.items()},
        "prefixes": results,
        "price_quotes": deepcopy(prices),
        "invoice_verified": False,
        "not_three_independent_1_2_4_experiments": True,
    }
    if include_known_buckets:
        result["known_bucket_reference"] = {key: known_bucket_cost(rows, prices) for key, rows in total.items()}
        result["cache_write_pricing"] = "Chat Completions cache-write usage is unreported by the observer; reference subtotals omit any write-rate adjustment."
    return result


def answer_review_request(case: dict[str, Any], sources: list[dict[str, Any]], answers: list[dict[str, Any]], case_index: int) -> tuple[dict[str, Any], dict[str, str]]:
    if len(answers) != 2 * REPETITIONS or any("response" not in row for row in answers):
        raise ValueError("reuse_answer_review_requires_eight_actual_answers")
    items: dict[str, Any] = {}
    mapping: dict[str, str] = {}
    for index in range(1, REPETITIONS + 1):
        for position, mode in enumerate(pair_order(case_index, index), start=1):
            row = next(item for item in answers if item["reuse_index"] == index and item["mode"] == mode)
            key = f"reuse-{index}-variant-{position}"
            mapping[key] = mode
            items[key] = {
                "reuse_index": index,
                "same_question_repeat": True,
                "context": json.loads(row["prepared"]["request"]["messages"][1]["content"]),
                "question": case["question"],
                "answer": row["response"]["answer"],
                "answer_record_sha256": original.digest(row),
                "rubric": deepcopy(case["rubric"]["answer"]),
            }
    return {
        "schema_version": 1,
        "synthetic_only": True,
        "kind": "reuse_answers",
        "case_id": case["id"],
        "sources": deepcopy(sources),
        "same_question_repeated_four_times": True,
        "not_fully_blind": True,
        "review_items": items,
    }, mapping


def blind_answer_review(case_id: str, sources: list[dict[str, Any]], answers: list[dict[str, Any]], trajectory: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Keep branch, request shape and call order outside the reviewer packet."""
    slots = {(row["reuse_index"], row["mode"]) for row in answers}
    expected = {(index, mode) for index in range(1, REPETITIONS + 1) for mode in original.MODES}
    if slots != expected or len(answers) != len(expected) or any("response" not in row for row in answers):
        raise ValueError("reuse_answer_review_requires_eight_actual_answers")
    items: dict[str, Any] = {}
    mapping: dict[str, dict[str, Any]] = {}
    for row in answers:
        key = secrets.token_hex(16)
        if key in mapping:
            raise RuntimeError("duplicate_review_item_identity")
        index = row["reuse_index"]
        mapping[key] = {"reuse_index": index, "mode": row["mode"], "answer_record_sha256": original.digest(row)}
        items[key] = {**deepcopy(trajectory[index - 1]), "answer": row["response"]["answer"]}
    return {
        "schema_version": 1,
        "synthetic_only": True,
        "kind": "reuse_answers",
        "case_id": case_id,
        "sources": [{"source_id": source["source_id"], "content": source["content"]} for source in sources],
        "review_items": {key: items[key] for key in sorted(items)},
    }, mapping


def semantic_prefixes(mapping: dict[str, dict[str, Any]], judgments: dict[str, Any]) -> dict[str, Any]:
    """Project the same bound decisions to consumption prefixes without imputing outcomes."""
    outcomes = {(item["reuse_index"], item["mode"]): judgments[key] for key, item in mapping.items() if key in judgments}
    results: dict[str, Any] = {}
    for count in PREFIXES:
        by_mode: dict[str, Any] = {}
        for mode in original.MODES:
            judged = [outcomes[(index, mode)] for index in range(1, count + 1) if (index, mode) in outcomes]
            by_mode[mode] = {
                "judged_answers": len(judged),
                "accepted_answers": sum(result["accepted"] for result in judged),
                "criteria": {
                    category: {result: sum(value == result for outcome in judged for value in outcome[category].values()) for result in ("satisfied", "violated", "unassessable")}
                    for category in ("required", "forbidden")
                },
            }
        compared = [
            (outcomes[(index, "uncompressed")]["accepted"], outcomes[(index, "compacted")]["accepted"])
            for index in range(1, count + 1)
            if (index, "uncompressed") in outcomes and (index, "compacted") in outcomes
        ]
        results[str(count)] = {
            "expected_pairs": count,
            "evaluated_pairs": len(compared),
            "complete_pairs": len(compared) == count,
            "by_mode": by_mode,
            "pair_outcomes": {
                "both_accepted": compared.count((True, True)),
                "raw_only_accepted": compared.count((True, False)),
                "compacted_only_accepted": compared.count((False, True)),
                "both_rejected": compared.count((False, False)),
            },
        }
    return results


def run_case(env: dict[str, Any], patch: pytest.MonkeyPatch, case: dict[str, Any], case_index: int, directory: Path, *, prices: dict[str, Any] | None = None) -> dict[str, Any]:
    expansion = "reuse_kind" in case
    if expansion:
        checked, trajectory = expansion_trajectory(case)
    else:
        checked = original.validate_case(case)
        trajectory = [{"question": checked["question"], "rubric": checked["rubric"]["answer"]}] * REPETITIONS
    same_question = not expansion or case["reuse_kind"] == "repeat"
    observer, journal, mid, label = env["observer"], env["journal"], env["memory_id"], case["id"]
    observer.max_attempts = MAX_SDK_PER_CASE
    report: dict[str, Any] = {
        "case_id": label,
        "case_sha256": original.digest(case),
        "synthetic_only": True,
        "controlled_protocol_only": env.get("reuse_controlled_protocol_only") is True,
        "mechanism_complete": False,
        "same_question_four_repeats": same_question,
        "applied_to_chat": False,
        "long_term_publication": False,
        "model_call_plan": {"source_embedding": 2, "summary": 1, "answers": 8, "SDK_maximum": MAX_SDK_PER_CASE, "completion_reserve": COMPLETION_RESERVE_PER_CASE},
        "sources": [],
        "answers": [],
        "consumer_adopted_for_evaluation_only": False,
    }
    if expansion:
        report.update(reuse_kind=case["reuse_kind"], semantic_summary_accepted=None, semantic_prefixes=semantic_prefixes({}, {}))
    started = time.monotonic()
    try:
        report["phase"] = "source_preparation"
        source_preflight = original.preflight_case(checked)
        report["source_preflight"] = {key: value for key, value in source_preflight.items() if key not in {"sdk_maximum", "completion_reserve_tokens"}}
        source_started = time.monotonic()
        for index, source in enumerate(checked["sources"]):
            original.append_source(env, source, f"{label}-reuse-source-{index}", report["sources"])
        report["shared_source_preparation_elapsed_seconds"] = time.monotonic() - source_started
        identities = [report["sources"][index]["source_id"] for index in checked["selected_source_indices"] + checked["retained_source_indices"]]
        compaction = original.compaction_request(identities, label + "-reuse-compact")
        report["compaction_request"] = compaction
        observer.set_stage("analysis", phase=label + "-reuse-summary")
        report["phase"] = "summary_generation"
        before = len(observer.attempts)
        summary_started = time.monotonic()
        with original.capture_summary(env, patch, report["sources"], compaction) as capture:
            report["summary_request_capture"] = capture
            generated = call(env, "POST", f"/memories/{mid}/context-compactions", json=compaction)
        report["summary_generation_elapsed_seconds"] = time.monotonic() - summary_started
        report["generated"] = generated
        assert generated["operation"]["status"] == "generated" and len(observer.attempts) - before == 1
        packages = original.comparison_packages(report["sources"], compaction, generated["artifact"])
        report["packages"] = packages
        reference = reference_from(generated)
        report["reference"] = reference
        report["phase"] = "independent_summary_review"
        requested = original.review_request(checked, "summary", {"artifact": {"sources": report["sources"], "artifact": generated["artifact"], "evaluation_only_no_application": True}})
        review = original.external_review(directory, label + "-reuse-summary", requested, journal, timeout_seconds=900)
        report["summary_review"] = {"request_sha256": original.digest(requested), "decision": review}
        summary_accepted = review["judgments"]["artifact"]["accepted"] is True
        if expansion:
            report["semantic_summary_accepted"] = summary_accepted
        assert summary_accepted, "compaction_summary_rejected_in_independent_review"
        report["consumer_adopted_for_evaluation_only"] = True

        def fetch(memory_id: str, operation_id: str) -> dict[str, Any]:
            return call(env, "GET", f"/memories/{memory_id}/context-compaction-operations/{operation_id}")

        report["phase"] = "repeated_answers"
        for index, step in enumerate(trajectory, start=1):
            for mode in pair_order(case_index, index, expansion=expansion):
                before = len(observer.attempts)
                read_started = time.monotonic()
                record = prepare_for_mode(reference, fetch, mode=mode, sources=report["sources"], compaction_request=compaction, expected_packages=packages, question=step["question"])
                assert len(observer.attempts) == before, "fresh_prepare_must_not_call_a_model"
                record.update(reuse_index=index, fresh_read_seconds=time.monotonic() - read_started)
                report["answers"].append(record)
                observer.set_stage("answer", phase=f"{label}-reuse-{index}-{mode}")
                send_started = time.monotonic()
                try:
                    record["response"] = asyncio.run(send_request(env, record))
                finally:
                    record["send_elapsed_seconds"] = time.monotonic() - send_started
        audit_path = directory / (label + ".reuse-variant-mapping.private.json")
        blind_mapping: dict[str, dict[str, Any]] = {}
        if expansion:
            requested, blind_mapping = blind_answer_review(label, report["sources"], report["answers"], trajectory)
            write_private(audit_path, {"mapping": blind_mapping, "review_request_sha256": original.digest(requested), "arm_masked": True}, journal)
        else:
            requested, mapping = answer_review_request(checked, report["sources"], report["answers"], case_index)
            write_private(audit_path, {"mapping": mapping, "not_fully_blind": True}, journal)
        report["phase"] = "independent_answer_review"
        review = original.external_review(directory, label + "-reuse-answers", requested, journal, timeout_seconds=900)
        report["answer_review"] = {"request_sha256": original.digest(requested), "decision": review}
        report["semantic_answers_accepted"] = all(item["accepted"] for item in review["judgments"].values())
        if expansion:
            report["semantic_prefixes"] = semantic_prefixes(blind_mapping, review["judgments"])
        report["phase"] = "withdrawal"
        state = call(env, "GET", f"/memories/{mid}/sources/{identities[0]}/state")
        report["withdrawal"] = call(
            env,
            "POST",
            f"/memories/{mid}/sources/{identities[0]}/withdrawals",
            json={"operation_id": label + "-reuse-withdraw", "expected_source_revision": int(state["source_revision"]), "reason": "Withdraw owned synthetic root after fixed repeat trajectory."},
        )
        before = len(observer.attempts)
        response = api_request(env, "GET", f"/memories/{mid}/context-compaction-operations/{reference['operation_id']}", actor=0)
        report["after_withdrawal"] = {"http_status": response.status_code, "body": response.json()}

        def unavailable(memory_id: str, operation_id: str) -> dict[str, Any]:
            assert memory_id == mid and operation_id == reference["operation_id"]
            # Fetch anew even after the independently recorded diagnostic read.
            return fetch(memory_id, operation_id)

        rejected = False
        try:
            prepare_compaction_request(
                reference,
                unavailable,
                scope={"user_id": original.SUBJECT, "agent_id": original.AGENT, "session_id": None},
                model="gpt-6.1-terra",
                question=checked["question"],
                context_window_tokens=original.CALLER_LIMIT,
                output_reserve_tokens=512,
                safety_margin_tokens=original.MARGIN,
                count_tokens=lambda text: len(encoder.encode(text, disallowed_special=())),
                tokenizer_label="cl100k_base_estimate",
            )
        except (ValueError, AssertionError):
            rejected = True
        assert rejected and len(observer.attempts) == before, "withdrawn_artifact_must_stop_before_model"
        assert packages["compacted"]["summary"] not in response.text
        report["withdrawal_prepare_rejected_no_model"] = True
        report["mechanism_complete"] = True
        return report
    except BaseException as error:
        report["error_code"] = type(error).__name__
        raise
    finally:
        report["elapsed_seconds_including_independent_review"] = time.monotonic() - started
        report["actual_attempts"] = deepcopy(observer.attempts)
        report["cost"] = costs(env)
        report["cost"]["planned_scope"] = ["source_embedding", "analysis", "answer"]
        report["cost"]["not_applicable"] = ["publication_embedding", "retrieval_embedding"]
        report["cumulative_cost"] = cumulative_cost(
            observer.attempts, report["answers"], summary_elapsed_seconds=report.get("summary_generation_elapsed_seconds"), prices=prices, include_known_buckets=expansion
        )
        report["not_claimed"] = ["fully blind trial", "business-human acceptance", "invoice/TCO", "cache inferred from latency", "production default injection", "applied or published context"]
        write_private(directory / (label + ".reuse-report.json"), report, journal)
