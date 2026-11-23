"""Run a bounded synthetic longitudinal comparison through the owned runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Memory, Task
from api.db.joint_services import memory_write_runtime
from api.db.memory_revision_models import MemoryEntryRevision
from api.db.memory_writer_models import MemoryWriterAdmission
from api.db.services.llm_service import LLMBundle
from common import settings
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.storage import ManagedMessageStore
from tests.support.memory_source_comparison import fixed_submission_clock
from tests.support.memory_startup_evaluation_cost import known_bucket_cost
from tests.support.memory_startup_longitudinal import AGENT, answer, canonical, costs, judgment
from tests.support.memory_target_longitudinal import candidates_for, wait_job
from tests.support.memory_workload_review import call, publish_reviewed, reviewed_candidates, write_private
from tests.support.memory_writes_http import api_request, result

ARMS = ("A", "B", "C", "D")
PHASES = ("initial", "corrected", "withdrawn")
SUBJECT = "synthetic-four-arm-subject"
MAX_ATTEMPTS = 60
MAX_ANALYSIS_OUTPUT = 1024
MAX_ANSWERS = len(ARMS) * len(PHASES)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_cases(path: Path, kind: str, *, expected_sha256: str | None = None) -> list[dict[str, Any]]:
    content = path.read_bytes()
    if expected_sha256 is not None and hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("four_arm_holdout_hash_mismatch")
    root = json.loads(content)
    if set(root) != {"schema_version", "kind", "cases"} or root["schema_version"] != 1 or root["kind"] != kind or len(root["cases"]) != 2:
        raise ValueError("invalid_four_arm_fixture")
    for row in root["cases"]:
        if set(row) != {"id", "initial", "correction", "replacement", "questions", "rubric"} or not re.fullmatch(r"[a-z][a-z0-9-]{0,36}", row["id"]):
            raise ValueError("invalid_four_arm_case")
        if len(row["initial"]) != 2 or set(row["questions"]) != set(PHASES) or set(row["rubric"]) != set(PHASES):
            raise ValueError("invalid_four_arm_trajectory")
        if any(not 1 <= len(criteria) <= 10 for criteria in row["rubric"].values()):
            raise ValueError("invalid_four_arm_rubric")
        texts = [*row["initial"], row["correction"], row["replacement"], *row["questions"].values(), *(criterion for criteria in row["rubric"].values() for criterion in criteria)]
        if any(not isinstance(text, str) or not text.strip() or len(text.encode()) > 4096 for text in texts):
            raise ValueError("invalid_four_arm_text")
    if len({row["id"] for row in root["cases"]}) != 2:
        raise ValueError("duplicate_four_arm_case")
    return root["cases"]


@contextmanager
def owned_queue(env: dict[str, Any], patch: pytest.MonkeyPatch) -> Iterator[str]:
    queue = "four-arm-eval-" + uuid4().hex
    redis = REDIS_CONN.REDIS
    assert not redis.exists(queue), "synthetic_queue_must_be_new"
    env["journal"].event("reserve-four-arm-queue", stream=queue, group="evaluation", consumer="evaluation")
    patch.setattr(settings, "get_svr_queue_name", lambda priority: queue)
    try:
        yield queue
    finally:
        if redis.exists(queue):
            redis.delete(queue)
        assert not redis.exists(queue)
        env["journal"].event("four-arm-queue-cleanup-readback", stream=queue, absent=True)


@contextmanager
def bounded_extraction(env: dict[str, Any], patch: pytest.MonkeyPatch) -> Iterator[None]:
    """Bound the evaluation's legacy caller without replacing extraction or its prompts."""
    original = LLMBundle.async_chat
    observer = env["observer"]
    observer.max_attempts = MAX_ATTEMPTS

    async def chat(bundle: LLMBundle, system: str, history: list[dict[str, Any]], generation: dict[str, Any] | None = None, **kwargs: Any) -> str:
        if observer.phase.endswith(":extract"):
            assert "max_completion_tokens" not in (generation or {})
            generation = {**(generation or {}), "max_completion_tokens": MAX_ANALYSIS_OUTPUT}
            kwargs["max_tokens"] = MAX_ANALYSIS_OUTPUT
        return await original(bundle, system, history, generation, **kwargs)

    with patch.context() as bounded:
        bounded.setattr(LLMBundle, "async_chat", chat)
        yield


def bank(env: dict[str, Any], arm: str) -> dict[str, Any]:
    result = call(
        env,
        "POST",
        "/memories",
        json={"name": "Synthetic four-arm " + arm, "memory_type": ["raw", "semantic"], "embd_id": "text-embedding-3-small@OpenAI", "llm_id": "gpt-6.1-terra@OpenAI"},
    )
    mid = result["id"]
    with Session(env["engine"]) as reader:
        record = reader.get(Memory, mid)
        assert record is not None and record.tenant_id == env["tenant_id"]
        assert (record.tenant_llm_id, record.tenant_embd_id) == (env["model_ids"]["chat"], env["model_ids"]["embedding"])
    env["journal"].event("reserve-four-arm-bank", arm=arm, memory_id=mid, tenant_id=env["tenant_id"])
    return dict(env, memory_id=mid, arm=arm)


def reconcile_append_transport(env: dict[str, Any], original: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Reconcile at most one confirmed transport failure on its original write."""
    if (
        original["status"] != "persistence_unknown"
        or original["index_sync_status"] != "unknown"
        or original.get("error_code") != "writer_io_unconfirmed"
        or len(attempts) != 1
        or attempts[0]["state"] != "failed"
        or attempts[0].get("error_code") != "APIConnectionError"
        or env["transport_recoveries"]
    ):
        return original
    mid, identity = env["memory_id"], original["operation_id"]
    endpoint = f"/memories/{mid}/operations/{identity}"
    observed = call(env, "GET", endpoint)
    assert (observed["status"], observed["index_sync_status"], observed.get("raw_source_id")) == ("persistence_unknown", "unknown", None)
    with Session(env["engine"]) as reader:
        writer = reader.get(MemoryWriterAdmission, (mid, identity))
        assert writer is not None and writer.kind == "append_raw" and writer.state == "drained" and writer.active_token is None and not writer.message_ids
    record = {"arm": env["arm"], "operation_id": identity, "failed_attempt_id": attempts[0]["attempt_id"], "original_status": observed["status"]}
    env["transport_recoveries"].append(record)
    reconciled = call(env, "POST", endpoint + "/reconciliations", json={})
    assert reconciled["operation_id"] == identity
    record["reconciled_status"] = reconciled["status"]
    return reconciled


def append_extract(env: dict[str, Any], patch: pytest.MonkeyPatch, queue: str, text: str, index: int) -> dict[str, Any]:
    observer, mid = env["observer"], env["memory_id"]
    label = f"{env['arm']}:source-{index}"
    observer.set_stage("source_embedding", phase=label)
    attempt_start = len(observer.attempts)
    with fixed_submission_clock(patch, index):
        response = api_request(
            env,
            "POST",
            f"/memories/{mid}/messages",
            token=env["keys"][0],
            json={"operation_id": f"source-{index}", "user_input": text, "agent_response": "", "user_id": SUBJECT, "agent_id": AGENT, "session_id": f"session-{index}"},
        )
    assert response.status_code in {200, 202}, response.text
    row = result(response, status=response.status_code)
    if response.status_code == 202:
        row = reconcile_append_transport(env, row, observer.attempts[attempt_start:])
    assert row["status"] == row["index_sync_status"] == "ready" and row["queue_state"] == "submitted"
    source, task_id = str(row["raw_source_id"]), row["extraction_task_id"]
    redis = REDIS_CONN.REDIS
    if not redis.xinfo_groups(queue):
        assert redis.xgroup_create(queue, "evaluation", id="0-0")
    stream = redis.xreadgroup("evaluation", "evaluation", {queue: ">"}, count=1)
    assert len(stream) == 1 and len(stream[0][1]) == 1
    delivery, fields = stream[0][1][0]
    payload = json.loads(fields.get("message", fields.get(b"message")))
    assert (payload["id"], payload["memory_id"], str(payload["source_id"]), payload["tenant_id"]) == (task_id, mid, source, env["tenant_id"])
    with Session(env["engine"]) as reader:
        persisted = reader.get(Task, task_id)
        assert persisted and persisted.task_type == "memory" and persisted.progress == 0 and persisted.digest == source
    observer.set_stage("analysis", phase=label + ":extract")
    assert asyncio.run(memory_write_runtime.handle_coordinated_task(payload))
    with Session(env["engine"]) as reader:
        persisted = reader.get(Task, task_id)
        assert persisted and persisted.progress == 1
    assert redis.xack(queue, "evaluation", delivery) == 1
    assert redis.xpending(queue, "evaluation")["pending"] == 0
    inventory = asyncio.run(ManagedMessageStore(env["tenant_id"], mid).inventory())
    derived = sorted(str(item["message_id"]) for item in inventory if item["message_type"] == "semantic" and str(item["source_id"]) == source)
    assert any(str(item["message_id"]) == source and item["content"] == f"User Input: {text}\nAgent Response: " for item in inventory)
    if env["arm"] != "A":
        current = call(env, "GET", f"/memories/{mid}/sources/{source}/state")
        call(env, "POST", f"/memories/{mid}/sources/{source}/adoptions", json={"operation_id": f"adopt-{index}", "expected_content_sha256": current["content_sha256"]})
    return {"source_id": source, "task_id": task_id, "derived": derived, "input_sha256": digest(text), "queue_acked": True}


def withdraw(env: dict[str, Any], source: dict[str, Any], label: str) -> dict[str, Any]:
    mid, source_id = env["memory_id"], source["source_id"]
    if env["arm"] == "A":
        # The legacy supported path is part of the comparison.
        storage = ManagedMessageStore(env["tenant_id"], mid)
        identities = {source_id, *source["derived"]}

        def observed_state() -> dict[str, tuple[bool, Any, str]]:
            return {str(row["message_id"]): (row["status"], row["forget_at"], digest(row["content"])) for row in asyncio.run(storage.inventory()) if str(row["message_id"]) in identities}

        before = observed_state()
        attempts = len(env["observer"].attempts)
        response = api_request(env, "DELETE", f"/messages/{mid}:{source_id}", actor=0)
        body = response.json()
        assert response.status_code == 200 and body["retcode"] == 409 and body["retmsg"] == "source_adoption_required"
        assert set(before) == identities and observed_state() == before and len(env["observer"].attempts) == attempts
        return {"http_status": response.status_code, "retcode": 409, "reason": "source_adoption_required", "legacy_forget": False, "no_row_or_model_effect": True}
    current = call(env, "GET", f"/memories/{mid}/sources/{source_id}/state")
    result = call(
        env,
        "POST",
        f"/memories/{mid}/sources/{source_id}/withdrawals",
        json={"operation_id": label, "expected_source_revision": current["source_revision"], "reason": "Owner retracts a synthetic source."},
    )
    assert result["stop_supply"] == "withdrawn"
    excerpt = api_request(env, "GET", f"/memories/{mid}/sources/{source_id}", actor=0)
    assert excerpt.status_code == 404
    return result


def revise_current(env: dict[str, Any], source: dict[str, Any], replacement: str) -> dict[str, Any]:
    if not source["derived"]:
        return {"state": "no_extracted_target"}
    mid = env["memory_id"]
    # Adopt a projection actually extracted from the correction source so its
    # later withdrawal also invalidates the manually revised current content.
    adoption = call(env, "POST", f"/memories/{mid}/entries", json={"operation_id": "adopt-correction", "legacy_message_id": source["derived"][0]})
    entry = adoption["entry_id"]
    current = call(env, "GET", f"/memories/{mid}/entries/{entry}")
    env["observer"].set_stage("publication_embedding", phase=env["arm"] + ":manual-revision")
    revision = call(
        env,
        "POST",
        f"/memories/{mid}/entries/{entry}/revisions",
        json={"operation_id": "revise-correction", "expected_revision": current["current_revision"], "content": replacement, "reason": "Explicit owner correction bound to the current source."},
    )
    assert revision["status"] == revision["index_sync_status"] == "ready"
    updated = call(env, "GET", f"/memories/{mid}/entries/{entry}")
    assert updated["current"]["content"] == replacement and updated["current_revision"] == current["current_revision"] + 1
    return {"state": "revised", "entry_id": entry, "current_message_id": updated["current_message_id"], "revision": updated["current_revision"]}


def verify(env: dict[str, Any], entry: str, label: str, directory: Path, *, controlled: bool) -> None:
    applicability = "仅限具名合成对象、适用范围和已授权来源。"
    if not controlled:
        judgment(env, entry, "human_confirmed", label, applicability, directory, review_timeout_seconds=600)
        return
    base = f"/memories/{env['memory_id']}/entries/{entry}"
    view = call(env, "GET", base + "/verification-snapshot")
    call(
        env,
        "POST",
        base + "/verifications",
        json={
            "operation_id": label,
            "expected_head_version": view["head_version"],
            "expected_binding_sha256": view["binding_sha256"],
            "result": "human_confirmed",
            "method": "source_comparison",
            "applicability": applicability,
            "notes": "Controlled synthetic operator.",
            "evidence": [],
        },
    )


def published_revisions(env: dict[str, Any]) -> set[tuple[str, int]]:
    with Session(env["engine"]) as reader:
        revisions = sa.select(MemoryEntryRevision.entry_id, MemoryEntryRevision.revision).where(MemoryEntryRevision.memory_id == env["memory_id"])
        return {(entry_id, revision) for entry_id, revision in reader.execute(revisions)}


def dream(
    env: dict[str, Any],
    prefix: str,
    phase: str,
    sources: list[dict[str, Any]],
    directory: Path,
    *,
    controlled: bool,
) -> dict[str, Any]:
    mid = env["memory_id"]
    label = f"{prefix}-{phase}"
    ids = [item["source_id"] for item in sources]
    observer = env["observer"]
    started = time.monotonic()
    job = call(
        env,
        "POST",
        f"/memories/{mid}/maintenance-jobs",
        json={
            "operation_id": label + "-job",
            "source_ids": ids,
            "entry_ids": [],
            "policy": "preferences" if prefix == "closed-network-diagnostics" else "decisions",
            "input_budget_tokens": 8192,
            "output_budget_tokens": MAX_ANALYSIS_OUTPUT,
        },
    )
    before_revisions = published_revisions(env)
    observer.set_stage("analysis", phase="C:" + phase + ":dream")
    call(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/runs", json={"operation_id": label + "-run"})
    complete = wait_job(env, job["job_id"])
    assert complete["state"] == "ready", complete
    candidates = candidates_for(env, job["job_id"])
    assert all(candidate["state"] == "pending" for candidate in candidates)
    assert before_revisions == published_revisions(env), "dreaming_analysis_must_not_publish"
    before_review = time.monotonic()
    raw = [call(env, "GET", f"/memories/{mid}/sources/{identity}", params={"limit": 8192}) for identity in ids]
    selected = (
        (
            candidates[:1]
            if controlled
            else reviewed_candidates(
                directory,
                label,
                candidates,
                env["journal"],
                allow_empty=True,
                evidence={"actual_sources": raw, "purpose": "independent synthetic review; approve at most one candidate faithful to the actual active sources and phase"},
            )
        )
        if candidates
        else []
    )
    assert len(selected) <= 1
    observer.set_stage("publication_embedding", phase="C:" + phase + ":publish")
    operations = publish_reviewed(env, mid, selected)
    reviews_finished = time.monotonic()
    entries = [item["entry_id"] for operation in operations for item in operation.get("entries", [])]
    assert len(entries) == len(selected), operations
    projections = []
    for entry in entries:
        verify(env, entry, label + "-verify", directory, controlled=controlled)
        binding = call(env, "GET", f"/memories/{mid}/entries/{entry}/verification-snapshot")["binding"]
        projections.append({"entry_id": entry, "message_id": str(binding["entry"]["message_id"]), "source_ids": [str(row["source_id"]) for row in binding["sources"]]})
    return {
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "job_id": job["job_id"],
        "entries": entries,
        "projections": projections,
        "analysis_and_queue_seconds": before_review - started,
        "review_and_publish_seconds": reviews_finished - before_review,
        "verification_seconds": time.monotonic() - reviews_finished,
    }


def retrieve(env: dict[str, Any], phase: str, question: str) -> dict[str, Any]:
    env["observer"].set_stage("retrieval_embedding", phase=env["arm"] + ":" + phase)
    result = call(
        env,
        "POST",
        f"/memories/{env['memory_id']}/context",
        json={
            "mode": "query",
            "query": question,
            "budget_tokens": 4096,
            "max_items": 8,
            "candidate_limit": 24,
            "source_excerpt_chars": 0,
            "user_id": SUBJECT,
            "agent_id": AGENT,
            "similarity_threshold": 0,
        },
    )

    assert all(item["memory_id"] == env["memory_id"] and item["user_id"] == SUBJECT and item["agent_id"] == AGENT for item in result["items"])
    return result


def startup(env: dict[str, Any], entries: list[str]) -> dict[str, Any]:
    before = len(env["observer"].attempts)
    fetch = api_request(
        env,
        "POST",
        f"/memories/{env['memory_id']}/startup-context",
        actor=0,
        json={"user_id": SUBJECT, "agent_id": AGENT, "session_id": None, **({"entry_ids": entries} if entries else {}), "budget_tokens": 4096, "max_items": 8},
    )
    assert fetch.headers["cache-control"] == "no-store"
    bundle = result(fetch)
    assert bundle["mutates_memory"] is False and bundle["model_calls"] is False and len(env["observer"].attempts) == before
    assert bundle["usage"]["estimated_tokens"] <= bundle["usage"]["budget_tokens"]
    return bundle


def assert_stopped(context: dict[str, Any], source: dict[str, Any], revised: dict[str, Any], *, projections: list[str]) -> None:
    denied = {source["source_id"], *source["derived"], revised.get("current_message_id"), *projections}
    for item in context["items"]:
        assert item["message_id"] not in denied
        if "source" in item:
            locator = item["source"]["locator"]
            assert locator is None or locator["message_id"] not in denied
        for source_ref in item.get("sources", []):
            assert str(source_ref["source_id"]) not in denied


def answer_phase(
    banks: dict[str, dict[str, Any]],
    case: dict[str, Any],
    phase: str,
    entries: list[str],
    report: dict[str, Any],
    *,
    withdrawn: dict[str, dict[str, Any]] | None = None,
) -> None:
    question = case["questions"][phase]
    contexts, retrieval_seconds = {}, {}
    for arm, env in banks.items():
        started = time.monotonic()
        contexts[arm] = retrieve(env, phase, question)
        retrieval_seconds[arm] = time.monotonic() - started
    started = time.monotonic()
    package = startup(banks["C"], entries)
    startup_seconds = time.monotonic() - started
    if withdrawn is not None:
        for arm in ("B", "C"):
            item = withdrawn[arm]
            assert_stopped(contexts[arm], item["source"], item["revised"], projections=item["projections"])
        item = withdrawn["C"]
        assert_stopped(package, item["source"], item["revised"], projections=item["projections"])
    order = list(ARMS)
    random.Random(case["id"] + "-" + phase).shuffle(order)
    for arm in order:
        lane = "C" if arm == "D" else arm
        env = banks[lane]
        env["observer"].set_stage("answer", phase=arm + ":" + phase)
        started = time.monotonic()
        response = asyncio.run(answer(env, contexts[lane], package if arm == "D" else None, question))
        report["answers"].append(
            {
                "arm": arm,
                "phase": phase,
                **response,
                "answer_wall_seconds": time.monotonic() - started,
                "retrieval_wall_seconds": retrieval_seconds[lane],
                "startup_wall_seconds": startup_seconds if arm == "D" else None,
                "context_sha256": digest(contexts[lane]),
                "context": contexts[lane],
                "startup": package if arm == "D" else None,
            }
        )
    assert report["answers"][-1]["phase"] == phase


def cost_by_arm(env: dict[str, Any]) -> dict[str, Any]:
    prices_path = os.environ.get("MULTIRAG_MEMORY_PRICE_QUOTES")
    prices = json.loads(Path(prices_path).read_text()) if prices_path else None
    attempts = env["observer"].attempts
    lanes = {arm: [row for row in attempts if row["workload_phase"].split(":", 1)[0] == arm] for arm in ARMS}
    assert all(row["stage"] == "answer" for row in lanes["D"])
    lanes["D"] = [row for row in lanes["C"] if row["stage"] != "answer"] + lanes["D"]
    return {arm: known_bucket_cost(rows, prices) for arm, rows in lanes.items()}


def masked_evidence(row: dict[str, Any]) -> list[dict[str, str]]:
    pack = row["startup"] or {}
    by_id = {str(item["message_id"]): item["content"] for item in [*row["context"]["items"], *pack.get("items", [])]}
    return [{"message_id": identity, "content": by_id[identity]} for identity in sorted(by_id, key=int)]


def run_case(
    env: dict[str, Any],
    patch: pytest.MonkeyPatch,
    case: dict[str, Any],
    directory: Path,
    kind: str,
    *,
    controlled: bool,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "case_id": case["id"],
        "fixture_kind": kind,
        "case_sha256": digest(case),
        "mechanism_complete": False,
        "answers": [],
        "source_records": {},
        "max_attempts": MAX_ATTEMPTS,
        "controlled": controlled,
        "shared_preparation": "C and read-only D share the exact retrieval and prepared bank",
    }
    started = time.monotonic()
    report["transport_recoveries"] = env["transport_recoveries"] = []
    try:
        with owned_queue(env, patch) as queue, bounded_extraction(env, patch):
            banks = {arm: bank(env, arm) for arm in ("A", "B", "C")}
            for arm, memory in banks.items():
                report["source_records"][arm] = [append_extract(memory, patch, queue, text, index) for index, text in enumerate(case["initial"])]
            source_records = report["source_records"]
            initial = dream(banks["C"], case["id"], "initial", source_records["C"], directory, controlled=controlled)
            report["initial"] = initial
            answer_phase(banks, case, "initial", initial["entries"], report)
            correction_started = time.monotonic()
            for arm, memory in banks.items():
                source_records[arm].append(append_extract(memory, patch, queue, case["correction"], 2))
            revisions = {}
            for arm in ("B", "C"):
                revisions[arm] = revise_current(banks[arm], source_records[arm][-1], case["replacement"])
                withdraw(banks[arm], source_records[arm][0], "withdraw-previous")
            initial_pack = next(row["startup"] for row in report["answers"] if row["arm"] == "D" and row["phase"] == "initial")
            obsolete_id = source_records["C"][0]["source_id"]
            retired = {row["entry_id"] for row in initial_pack["items"] if any(str(ref["source_id"]) == obsolete_id for ref in row["sources"])}
            fresh = startup(banks["C"], initial["entries"])
            assert not retired.intersection(row["entry_id"] for row in fresh["items"])
            report["initial_verified_entries_invalidated"] = sorted(retired)
            report["revisions"] = revisions
            report["revision_workflow_seconds"] = time.monotonic() - correction_started
            corrected = dream(banks["C"], case["id"], "corrected", source_records["C"][1:], directory, controlled=controlled)
            report["corrected"] = corrected
            report["dreaming_correction_workflow_seconds"] = time.monotonic() - correction_started
            answer_phase(banks, case, "corrected", corrected["entries"], report)
            report["withdrawals"] = {arm: withdraw(memory, source_records[arm][-1], "withdraw-current") for arm, memory in banks.items()}
            removed_projections = [row["message_id"] for row in corrected["projections"] if source_records["C"][-1]["source_id"] in row["source_ids"]]
            report["withdrawn_dreaming_projections_checked"] = removed_projections
            answer_phase(
                banks,
                case,
                "withdrawn",
                corrected["entries"],
                report,
                withdrawn={arm: {"source": source_records[arm][-1], "revised": revisions[arm], "projections": removed_projections if arm == "C" else []} for arm in ("B", "C")},
            )
            assert len(report["answers"]) == MAX_ANSWERS
            assert {tuple(row["input_sha256"] for row in records) for records in source_records.values()} == {tuple(digest(text) for text in [*case["initial"], case["correction"]])}
            for phase in PHASES:
                paired = [row["context_sha256"] for row in report["answers"] if row["phase"] == phase and row["arm"] in {"C", "D"}]
                assert len(paired) == 2 and paired[0] == paired[1]
            report["mechanism_complete"] = True
    except BaseException as error:
        report["error_code"] = type(error).__name__
        raise
    finally:
        report.update(elapsed_seconds=time.monotonic() - started, cost=costs(env), deployment_arm_cost=cost_by_arm(env))
        write_private(directory / f"{case['id']}.four-arm-report.json", report, env["journal"])
        if report["answers"] and not controlled:
            packet = []
            mapping = {}
            for row in report["answers"]:
                identifier = uuid4().hex
                mapping[identifier] = {"arm": row["arm"], "phase": row["phase"]}
                packet.append(
                    {
                        "id": identifier,
                        "phase": row["phase"],
                        "question": case["questions"][row["phase"]],
                        "criteria": case["rubric"][row["phase"]],
                        "answer": row["answer"],
                        "available_evidence": masked_evidence(row),
                    }
                )
            random.Random(case["id"] + "-review").shuffle(packet)
            write_private(directory / f"{case['id']}.four-arm-review.json", {"synthetic_only": True, "items": packet}, env["journal"])
            write_private(directory / f"{case['id']}.four-arm-mapping.json", mapping, env["journal"])
    return report
