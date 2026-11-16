"""Candidate-only, paired source representations; semantic judgment stays external."""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal, Self

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.services import memory_source_service
from api.db.joint_services import memory_write_runtime
from api.db.memory_maintenance_models import MemoryMaintenanceInput, MemoryMaintenanceJob
from api.db.services.llm_service import LLMBundle
from common.token_utils import encoder
from memory.maintenance.domain import POLICIES
from memory.maintenance.jobs import manifest_for_job
from memory.maintenance.provider import _SYSTEM
from memory.maintenance.snapshots import analysis_payload
from memory.revisions.domain import content_hash
from memory.structured_sources import canonical_source, declared_event_time, source_envelope
from tests.support.memory_workload_review import call, write_private
from tests.support.memory_writes_http import api_request, result

Branch = Literal["legacy", "structured"]
BRANCHES: tuple[Branch, Branch] = ("legacy", "structured")
SUBJECT, AGENT = "source-comparison-subject", "source-comparison-agent"
INPUT_BUDGET, OUTPUT_BUDGET = 8192, 1024
MAX_CASES, MAX_SOURCES = 6, 4
BLIND_SEED = 20261116140439
REFERENCE_TIME = datetime(2026, 11, 16, 12, tzinfo=UTC)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def branch_order(case_index: int) -> tuple[Branch, Branch]:
    """Alternate the first treatment after one frozen seeded starting choice."""
    if type(case_index) is not int or not 0 <= case_index < MAX_CASES:
        raise ValueError("invalid_comparison_case_index")
    start = random.Random(BLIND_SEED).randrange(2)
    return BRANCHES if (case_index + start) % 2 == 0 else (BRANCHES[1], BRANCHES[0])


def validate_case(value: Any) -> dict[str, Any]:
    """Validate a synthetic fixture without deriving answers or changing its text."""
    fields = {"id", "policy", "sources", "rubric"}
    if not isinstance(value, dict) or not fields <= set(value) or set(value) - fields - {"expected_source_indices"}:
        raise ValueError("invalid_comparison_case")
    if not isinstance(value["id"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", value["id"]) or value["policy"] not in POLICIES:
        raise ValueError("invalid_comparison_identity")
    sources = value["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise ValueError("invalid_comparison_sources")
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"session_id", "user_input", "agent_response", "event_time"}:
            raise ValueError("invalid_comparison_source")
        if not isinstance(source["session_id"], str) or len(source["session_id"].encode()) > 256:
            raise ValueError("invalid_comparison_session")
        if any(not isinstance(source[name], str) or not source[name].strip() for name in ("user_input", "agent_response")):
            raise ValueError("invalid_comparison_text")
        declared_event_time(source["event_time"])
        # A timestamp supplied only to B would be an additional semantic fact.
        if source["event_time"] is not None and source["event_time"] not in source["user_input"] + source["agent_response"]:
            raise ValueError("comparison_event_time_not_in_both_plaintexts")
        if len(expected_content(source, "structured")) > 8192:
            raise ValueError("comparison_source_exceeds_full_excerpt_limit")
    rubric = value["rubric"]
    if not isinstance(rubric, dict) or set(rubric) != {"required", "forbidden"}:
        raise ValueError("invalid_comparison_rubric")
    identifiers: list[str] = []
    for rules in rubric.values():
        if not isinstance(rules, list) or not 1 <= len(rules) <= 12:
            raise ValueError("invalid_comparison_criteria")
        for rule in rules:
            if not isinstance(rule, dict) or set(rule) != {"id", "description"} or not isinstance(rule["id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", rule["id"]):
                raise ValueError("invalid_comparison_criterion")
            if not isinstance(rule["description"], str) or not rule["description"].strip() or len(rule["description"].encode()) > 4096:
                raise ValueError("invalid_comparison_criterion")
            identifiers.append(rule["id"])
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("duplicate_comparison_criterion")
    if "expected_source_indices" in value:
        indices = value["expected_source_indices"]
        if not isinstance(indices, list) or not indices or any(type(i) is not int or not 0 <= i < len(sources) for i in indices) or len(set(indices)) != len(indices):
            raise ValueError("invalid_comparison_source_indices")
    return deepcopy(value)


def load_cases(path: Path, *, expected_kind: str) -> list[dict[str, Any]]:
    document = json.loads(path.read_text())
    if set(document) != {"schema_version", "kind", "cases"} or type(document["schema_version"]) is not int or document["schema_version"] != 1 or document["kind"] != expected_kind:
        raise ValueError("invalid_comparison_fixture")
    cases = [validate_case(case) for case in document["cases"]]
    if not cases or len(cases) > MAX_CASES or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("invalid_comparison_case_inventory")
    return cases


def source_request(source: dict[str, Any], branch: Branch, operation_id: str) -> dict[str, Any]:
    shared = {"operation_id": operation_id, "user_id": SUBJECT, "agent_id": AGENT, "session_id": source["session_id"]}
    if branch == "legacy":
        return {**shared, "user_input": source["user_input"], "agent_response": source["agent_response"]}
    if branch != "structured":
        raise ValueError("invalid_comparison_branch")
    return {**shared, "segments": [{"role": "user", "content": source["user_input"]}, {"role": "assistant", "content": source["agent_response"]}], "event_time": source["event_time"]}


def expected_content(source: dict[str, Any], branch: Branch) -> str:
    if branch == "legacy":
        return f"User Input: {source['user_input']}\nAgent Response: {source['agent_response']}"
    request = source_request(source, branch, "fixture-content")
    return canonical_source(source_envelope(request["segments"], request["event_time"]))


def verify_source_pair(source: dict[str, Any], legacy: dict[str, Any], structured: dict[str, Any]) -> None:
    for branch, observed in zip(BRANCHES, (legacy, structured), strict=True):
        if observed["content"] != expected_content(source, branch) or content_hash(observed["content"]) != observed["content_sha256"]:
            raise ValueError("comparison_actual_source_changed")
        if observed["scope"] != {"user_id": SUBJECT, "agent_id": AGENT, "session_id": source["session_id"]}:
            raise ValueError("comparison_scope_changed")
    if legacy["valid_at"] != structured["valid_at"]:
        raise ValueError("comparison_reference_time_changed")


@contextmanager
def fixed_submission_clock(patch: pytest.MonkeyPatch, index: int) -> Iterator[None]:
    """Same server reference instant for each pair; production code stays intact."""
    instant = REFERENCE_TIME + timedelta(minutes=index)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(instant.timestamp(), tz)

    with patch.context() as scoped:
        scoped.setattr(memory_source_service, "datetime", Clock)
        scoped.setattr(memory_write_runtime, "current_timestamp", lambda: int(instant.timestamp() * 1000))
        yield


@contextmanager
def capture_analyst(env: dict[str, Any], patch: pytest.MonkeyPatch) -> Iterator[None]:
    original = LLMBundle.async_chat

    async def inspect(bundle: LLMBundle, system: str, history: list[dict[str, Any]], generation: dict[str, Any], **options: Any) -> Any:
        expected = env["comparison_expected_payload"]
        assert system == _SYSTEM and history == [{"role": "user", "content": expected}], "comparison_model_payload_drift"
        caller_request = {"system": system, "history": deepcopy(history), "generation": deepcopy(generation), "request_options": deepcopy(options)}
        env["comparison_requests"].append(
            {
                "phase": env["observer"].phase,
                "system_sha256": content_hash(system),
                "body": json.loads(expected),
                "body_sha256": content_hash(expected),
                "body_estimated_tokens": len(encoder.encode(expected, disallowed_special=())),
                "generation": deepcopy(generation),
                "request_options": deepcopy(options),
                "system_estimated_tokens": len(encoder.encode(system, disallowed_special=())),
                "caller_request": caller_request,
                "caller_request_sha256": digest(caller_request),
                "caller_request_estimated_tokens": len(encoder.encode(canonical(caller_request), disallowed_special=())),
                "tokenizer_basis": "existing cl100k_base estimate; actual provider usage is reported separately; SDK framing may differ",
            }
        )
        return await original(bundle, system, history, generation, **options)

    with patch.context() as scoped:
        scoped.setattr(LLMBundle, "async_chat", inspect)
        yield


def _manifest(env: dict[str, Any], job_id: str) -> dict[str, Any]:
    with Session(env["engine"]) as db:
        db.execute(sa.text("SET TRANSACTION READ ONLY"))
        job = db.get(MemoryMaintenanceJob, job_id)
        assert job is not None and job.memory_id == env["memory_id"]
        inputs = list(db.scalars(sa.select(MemoryMaintenanceInput).where(MemoryMaintenanceInput.job_id == job_id).order_by(MemoryMaintenanceInput.input_id)))
        return manifest_for_job(job, inputs)


def run_branch(env: dict[str, Any], patch: pytest.MonkeyPatch, case: dict[str, Any], branch: Branch) -> dict[str, Any]:
    mid, label = env["memory_id"], case["id"] + "-" + branch
    outcome: dict[str, Any] = {"branch": branch, "state": "preparing", "sources": [], "candidates": [], "payload": None}
    env["comparison_branches"][branch] = outcome
    for index, source in enumerate(case["sources"]):
        request = source_request(source, branch, f"{label}-source-{index}")
        env["observer"].set_stage("source_embedding", phase=f"{label}-source-{index}")
        path = f"/memories/{mid}/" + ("messages" if branch == "legacy" else "sources")
        with fixed_submission_clock(patch, index):
            response = api_request(env, "POST", path, token=env["keys"][0], json=request)
        row: dict[str, Any] = {"operation_id": request["operation_id"], "http_status": response.status_code, "response": response.json()}
        outcome["sources"].append(row)
        if response.status_code not in {200, 202} or row["response"].get("retcode") != 0:
            outcome.update(state="source_rejected")
            return outcome
        operation = result(response, status=response.status_code)
        row["operation"] = operation
        if operation.get("status") != "ready" or operation.get("index_sync_status") != "ready":
            outcome.update(state="source_unsettled")
            return outcome
        identity = str(operation["raw_source_id"])
        row["source_id"] = identity
        state = call(env, "GET", f"/memories/{mid}/sources/{identity}/state")
        call(env, "POST", f"/memories/{mid}/sources/{identity}/adoptions", json={"operation_id": request["operation_id"] + "-adopt", "expected_content_sha256": state["content_sha256"]})
        provenance = call(env, "GET", f"/memories/{mid}/sources/{identity}/provenance", params={"include_segments": True})
        actual = call(
            env,
            "GET",
            f"/memories/{mid}/sources/{identity}",
            params={
                "offset": 0,
                "limit": 8192,
                "expected_source_revision": provenance["source_revision"],
                "expected_source_generation": provenance["source_generation"],
                "content_sha256": provenance["content_sha256"],
                **provenance["scope"],
            },
        )
        assert actual["state"] == "available" and actual["truncated"] is False and actual["excerpt"] == expected_content(source, branch)
        row.update(content=actual["excerpt"], content_sha256=actual["content_sha256"], scope=actual["scope"], valid_at=provenance["reference_time"], provenance=provenance, state=state)
    source_ids = [row["source_id"] for row in outcome["sources"]]
    request = {"operation_id": label + "-job", "source_ids": source_ids, "entry_ids": [], "policy": case["policy"], "input_budget_tokens": INPUT_BUDGET, "output_budget_tokens": OUTPUT_BUDGET}
    response = api_request(env, "POST", f"/memories/{mid}/maintenance-jobs", actor=0, json=request)
    outcome.update(job_request=request, create_response={"status": response.status_code, "body": response.json()})
    if response.status_code not in {200, 202} or response.json().get("retcode") != 0:
        outcome.update(state="job_rejected")
        return outcome
    job = result(response, status=response.status_code)
    manifest = _manifest(env, job["job_id"])
    payload = analysis_payload(manifest, case["policy"])
    outcome.update(job=job, manifest=manifest, payload=payload, payload_sha256=digest(payload))
    complete = (
        not manifest["entries"]
        and len(manifest["inputs"]) == len(case["sources"])
        and {str(item["source_id"]) for item in manifest["inputs"]} == set(source_ids)
        and all(item["coverage"] == "included" and item.get("fragment") is None for item in manifest["inputs"])
    )
    outcome["complete_model_source_coverage"] = complete
    if not complete:
        outcome["state"] = "source_coverage_not_comparable"
        return outcome
    env["comparison_expected_payload"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    env["observer"].set_stage("analysis", phase=label + "-analysis")
    submitted = api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/runs", actor=0, json={"operation_id": label + "-run"})
    outcome["run_response"] = {"status": submitted.status_code, "body": submitted.json()}
    if submitted.status_code not in {200, 202} or submitted.json().get("retcode") != 0:
        outcome["state"] = "submission_rejected"
        return outcome
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        assert not env["worker_errors"], [type(error).__name__ for error in env["worker_errors"]]
        current = call(env, "GET", f"/memories/{mid}/maintenance-jobs/{job['job_id']}")
        outcome["settled_job"] = current
        execution = current.get("execution") or {}
        if execution.get("state") in {"succeeded", "failed", "canceled", "needs_reconciliation"}:
            outcome["state"] = "succeeded" if execution["state"] == "succeeded" and current["state"] == "ready" else "analysis_failed"
            outcome["candidates"] = call(env, "GET", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/candidates")["candidates"]
            assert all(item["assessment"] == {"kind": "hypothesis", "verification_status": "unverified"} for item in outcome["candidates"])
            return outcome
        time.sleep(0.1)
    outcome["state"] = "analysis_unsettled"
    return outcome


def review_request(case: dict[str, Any], report: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Opaque variants preserve candidate wording; raw branches stay in a sidecar."""
    order = list(BRANCHES)
    random.Random(str(BLIND_SEED) + ":" + case["id"]).shuffle(order)
    variants: dict[str, Any] = {}
    mapping: dict[str, str] = {}
    for ordinal, branch in enumerate(order):
        alias = f"variant-{ordinal + 1}"
        mapping[alias] = branch
        actual = report["branches"][branch]
        sources = {row["source_id"]: index for index, row in enumerate(actual["sources"]) if "source_id" in row}
        views: list[dict[str, Any]] = []
        for candidate in actual["candidates"]:
            views.append(
                {
                    "candidate": len(views),
                    "action": candidate["action"],
                    "content": candidate["content"],
                    "source_indices": [sources[str(ref["source_id"])] for ref in candidate["sources"]],
                    "reason": candidate["reason"],
                    "assessment": deepcopy(candidate["assessment"]),
                }
            )
        variants[alias] = {"analysis_state": actual["state"], "raw_candidate_sha256": digest(actual["candidates"]), "candidates": views}
    request = {
        "case_id": case["id"],
        "synthetic_only": True,
        "report_sha256": digest(report),
        "source_texts": [{key: source[key] for key in ("session_id", "user_input", "agent_response")} for source in case["sources"]],
        "rubric": deepcopy(case["rubric"]),
        "expected_source_indices": case.get("expected_source_indices"),
        "variants": variants,
        "instruction": "Judge supported facts, missing facts, incorrect attribution, uncertainty and citation closure. Empty proposals or analysis failure do not imply factual correctness. No candidate approval or publication is requested.",
    }
    request["review_sha256"] = digest(request)
    return request, {"case_id": case["id"], "review_sha256": request["review_sha256"], "report_sha256": request["report_sha256"], "variants": mapping}


def validate_review(request: dict[str, Any], decision: Any) -> dict[str, Any]:
    sealed = {key: value for key, value in request.items() if key != "review_sha256"}
    if request.get("review_sha256") != digest(sealed):
        raise ValueError("comparison_review_request_changed")
    if (
        not isinstance(decision, dict)
        or set(decision) != {"review_sha256", "reviewed_by", "synthetic_only", "variants"}
        or decision["review_sha256"] != request["review_sha256"]
        or not isinstance(decision["reviewed_by"], str)
        or not decision["reviewed_by"].strip()
        or decision["synthetic_only"] is not True
    ):
        raise ValueError("comparison_independent_review_binding_invalid")
    if not isinstance(decision["variants"], dict) or set(decision["variants"]) != set(request["variants"]):
        raise ValueError("comparison_review_variants_invalid")
    for view in decision["variants"].values():
        if not isinstance(view, dict) or set(view) != {"required", "forbidden", "citation_closure", "uncertainty", "notes"}:
            raise ValueError("comparison_review_shape_invalid")
        for kind, allowed in (("required", {"supported", "partial", "missing", "contradicted", "unassessable"}), ("forbidden", {"absent", "present", "unassessable"})):
            if not isinstance(view[kind], dict) or set(view[kind]) != {rule["id"] for rule in request["rubric"][kind]} or any(score not in allowed for score in view[kind].values()):
                raise ValueError("comparison_review_criteria_invalid")
        if (
            view["citation_closure"] not in {"supported", "partial", "unsupported", "unassessable"}
            or view["uncertainty"] not in {"preserved", "overstated", "unassessable"}
            or not isinstance(view["notes"], str)
            or not view["notes"].strip()
        ):
            raise ValueError("comparison_review_judgment_invalid")
    return deepcopy(decision)


def save_review(directory: Path, case: dict[str, Any], report: dict[str, Any], env: dict[str, Any]) -> None:
    request, mapping = review_request(case, report)
    write_private(directory / (case["id"] + ".raw-report.json"), report, env["journal"])
    write_private(directory / (case["id"] + ".semantic-review-request.json"), request, env["journal"])
    sidecar = directory / "blinding"
    if not sidecar.exists():
        env["journal"].event("reserve-blinding-sidecar-directory", path=str(sidecar))
        sidecar.mkdir(mode=0o700)
    write_private(sidecar / (case["id"] + ".mapping.json"), mapping, env["journal"])
