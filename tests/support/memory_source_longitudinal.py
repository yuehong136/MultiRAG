"""Owned two-bank source replay with explicit external adoption and confirmation."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Base, Memory, Task
from api.db.memory_maintenance_models import MemoryMaintenanceInput, MemoryMaintenanceJob
from core.utils.redis_conn import REDIS_CONN
from memory.maintenance.jobs import manifest_for_job
from memory.maintenance.snapshots import analysis_payload
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import index_name
from tests.support.memory_source_comparison import (
    AGENT,
    BRANCHES,
    INPUT_BUDGET,
    OUTPUT_BUDGET,
    SUBJECT,
    Branch,
    branch_order,
    capture_analyst,
    digest,
    expected_content,
    fixed_submission_clock,
    review_request,
    source_request,
    validate_review,
    verify_source_pair,
)
from tests.support.memory_source_comparison import (
    validate_case as validate_comparison_case,
)
from tests.support.memory_startup_longitudinal import answer, costs, fingerprint, judgment
from tests.support.memory_workload_review import call, write_private
from tests.support.memory_writes_http import api_request, result

Answerer = Callable[[dict[str, Any], dict[str, Any], dict[str, Any] | None, str], Coroutine[Any, Any, dict[str, Any]]]


def validate_case(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"id", "policy", "applicability", "initial", "correction", "questions", "rubric"}:
        raise ValueError("invalid_longitudinal_case")
    if not isinstance(value["applicability"], str) or not value["applicability"].strip() or len(value["applicability"].encode()) > 2048:
        raise ValueError("invalid_longitudinal_applicability")
    questions, rubric = value["questions"], value["rubric"]
    if not isinstance(questions, dict) or not isinstance(rubric, dict) or set(questions) != {"initial", "corrected", "withdrawn"} or set(rubric) != set(questions):
        raise ValueError("invalid_longitudinal_phases")
    corrected = value["correction"] is not None
    if (questions["corrected"] is not None) != corrected or (rubric["corrected"] is not None) != corrected:
        raise ValueError("invalid_longitudinal_correction")
    sources = [value["initial"], *([value["correction"]] if corrected else [])]
    for phase in ("initial", "corrected", "withdrawn"):
        if phase == "corrected" and not corrected:
            continue
        if not isinstance(questions[phase], str) or not questions[phase].strip() or len(questions[phase].encode()) > 4096:
            raise ValueError("invalid_longitudinal_question")
        if not isinstance(rubric[phase], dict) or set(rubric[phase]) != ({"answer"} if phase == "withdrawn" else {"candidate", "answer"}):
            raise ValueError("invalid_longitudinal_rubric")
        for criteria in rubric[phase].values():
            validate_comparison_case({"id": value["id"], "policy": value["policy"], "sources": sources, "rubric": criteria})
    return deepcopy(value)


def load_cases(path: Path, kind: str) -> list[dict[str, Any]]:
    document = json.loads(path.read_text())
    if set(document) != {"schema_version", "kind", "cases"} or document["schema_version"] != 1 or document["kind"] != kind or not isinstance(document["cases"], list) or len(document["cases"]) != 1:
        raise ValueError("invalid_longitudinal_fixture")
    return [validate_case(item) for item in document["cases"]]


def planned_calls(case: dict[str, Any]) -> dict[str, int]:
    phases = 2 if case["correction"] is not None else 1
    return {"source_embedding": phases * 2, "publication_embedding": phases * 2, "retrieval_embedding": (phases + 1) * 2, "analysis": phases * 2, "answer": (phases + 1) * 2}


@contextmanager
def peer_banks(env: dict[str, Any]) -> Iterator[dict[Branch, dict[str, Any]]]:
    """Clone only fixture configuration; peer is explicitly reserved and removed."""
    tenant, engine, journal = env["tenant_id"], env["engine"], env["journal"]
    if not re.fullmatch(r"multirag_test_[0-9a-f]{12}", str(engine.url.database)):
        raise ValueError("peer_memory_requires_owned_scratch_database")
    peer = uuid4().hex
    with Session(engine) as reader:
        reader.execute(sa.text("SET TRANSACTION READ ONLY"))
        assert reader.get(Memory, peer) is None
    collection = index_name(tenant) + "_" + peer
    cache_keys = [peer, "memory_" + peer]
    journal.event("reserve-peer-memory", memory_id=peer, tenant_id=tenant, collection=collection, setup="SQL copy of owned RAW configuration")
    journal.event(
        "reserve-peer-memory-sql-scope",
        memory_id=peer,
        tables=[table.fullname for table in Base.metadata.sorted_tables if "memory_id" in table.c],
        model_ids=env.get("model_ids"),
        reuses_parent_owned_counter_queue=True,
    )
    journal.event("reserve-memory-index", memory_id=peer, tenant_id=tenant, collection=collection)
    for key in cache_keys:
        journal.event("reserve-redis-key", key=key)
    try:
        with Session(engine) as db:
            original = db.get(Memory, env["memory_id"])
            assert original is not None and original.tenant_id == tenant
            values = {column.key: deepcopy(getattr(original, column.key)) for column in Memory.__table__.columns}
            values.update(id=peer, name="Owned paired source replay peer")
            db.add(Memory(**values))
            db.commit()
        with Session(engine) as reader:
            original, copied = reader.get(Memory, env["memory_id"]), reader.get(Memory, peer)
            assert original is not None and copied is not None
            assert all(getattr(original, field) == getattr(copied, field) for field in ("tenant_id", "permissions", "memory_type", "tenant_embd_id", "tenant_llm_id"))
        yield {"legacy": dict(env), "structured": dict(env, memory_id=peer)}
    finally:
        original_error = sys.exception()
        errors: list[BaseException] = []
        env["worker_stop"].set()
        env["worker_thread"].join(timeout=40)
        if env["worker_thread"].is_alive():
            errors.append(AssertionError("longitudinal_worker_did_not_drain"))
            journal.event("peer-memory-cleanup-blocked", memory_id=peer, actual_worker_drained=False)
            raise BaseExceptionGroup("Peer cleanup requires actual writer drain", ([original_error] if original_error else []) + errors)
        try:
            asyncio.run(ManagedMessageStore(tenant, peer).drop())
        except BaseException as error:
            errors.append(error)
        from common import settings

        try:
            assert not settings.msgStoreConn.index_exists_strict(index_name(tenant), peer)
        except BaseException as error:
            errors.append(error)
        try:
            with Session(engine) as db:
                tables = [table for table in reversed(Base.metadata.sorted_tables) if table.schema == "usr_ai" and "memory_id" in table.c]
                for table in tables:
                    db.execute(sa.delete(table).where(table.c.memory_id == peer))
                db.execute(sa.delete(Task).where(Task.doc_id == peer))
                db.execute(sa.delete(Memory).where(Memory.id == peer))
                db.commit()
            with Session(engine) as reader:
                remaining = {table.fullname: reader.scalar(sa.select(sa.func.count()).select_from(table).where(table.c.memory_id == peer)) for table in tables}
                assert not any(remaining.values()) and reader.get(Memory, peer) is None
            journal.event("peer-memory-sql-cleanup-readback", memory_id=peer, remaining=remaining)
        except BaseException as error:
            errors.append(error)
        try:
            REDIS_CONN.REDIS.delete(*cache_keys)
            assert not REDIS_CONN.REDIS.exists(*cache_keys)
        except BaseException as error:
            errors.append(error)
        journal.event("peer-memory-cleanup-result", memory_id=peer, actual_worker_drained=True, errors=[type(error).__name__ for error in errors])
        if errors:
            raise BaseExceptionGroup("Peer Memory cleanup failed", ([original_error] if original_error else []) + errors)


def append_source(env: dict[str, Any], patch: pytest.MonkeyPatch, case: dict[str, Any], branch: Branch, phase: str, state: dict[str, Any]) -> dict[str, Any] | None:
    source = case["initial" if phase == "initial" else "correction"]
    label = case["id"] + "-" + branch + "-" + phase + "-source"
    request = source_request(source, branch, label)
    state.update(source_operation_id=label, state="source_submitting")
    env["observer"].set_stage("source_embedding", phase=label)
    path = f"/memories/{env['memory_id']}/" + ("messages" if branch == "legacy" else "sources")
    with fixed_submission_clock(patch, 0 if phase == "initial" else 1):
        response = api_request(env, "POST", path, token=env["keys"][0], json=request)
    state["source_response"] = {"http_status": response.status_code, "body": response.json()}
    if response.status_code not in {200, 202} or response.json().get("retcode") != 0:
        state["state"] = "source_rejected"
        return None
    operation = result(response, status=response.status_code)
    if operation.get("status") != "ready" or operation.get("index_sync_status") != "ready":
        state["state"] = "source_unsettled"
        return None
    identity = str(operation["raw_source_id"])
    current = call(env, "GET", f"/memories/{env['memory_id']}/sources/{identity}/state")
    call(env, "POST", f"/memories/{env['memory_id']}/sources/{identity}/adoptions", json={"operation_id": label + "-adopt", "expected_content_sha256": current["content_sha256"]})
    provenance = call(env, "GET", f"/memories/{env['memory_id']}/sources/{identity}/provenance", params={"include_segments": True})
    actual = call(
        env,
        "GET",
        f"/memories/{env['memory_id']}/sources/{identity}",
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
    value = {
        "source_id": identity,
        "content": actual["excerpt"],
        "content_sha256": actual["content_sha256"],
        "scope": actual["scope"],
        "valid_at": provenance["reference_time"],
        "provenance": provenance,
    }
    state.update(source=value, state="source_ready")
    return value


def analyze_sources(
    env: dict[str, Any], patch: pytest.MonkeyPatch, case: dict[str, Any], branch: Branch, phase: str, sources: list[dict[str, Any]], entry: dict[str, Any] | None, state: dict[str, Any]
) -> None:
    label = case["id"] + "-" + branch + "-" + phase
    request = {
        "operation_id": label + "-job",
        "source_ids": [item["source_id"] for item in sources],
        "entry_ids": [entry["entry_id"]] if entry else [],
        "policy": case["policy"],
        "input_budget_tokens": INPUT_BUDGET,
        "output_budget_tokens": OUTPUT_BUDGET,
    }
    response = api_request(env, "POST", f"/memories/{env['memory_id']}/maintenance-jobs", actor=0, json=request)
    state.update(job_request=request, job_response={"http_status": response.status_code, "body": response.json()}, sources=sources, candidates=[])
    if response.status_code not in {200, 202} or response.json().get("retcode") != 0:
        state["state"] = "job_rejected"
        return
    job = result(response, status=response.status_code)
    state["job"] = job
    with Session(env["engine"]) as db:
        db.execute(sa.text("SET TRANSACTION READ ONLY"))
        row = db.get(MemoryMaintenanceJob, job["job_id"])
        assert row is not None and row.memory_id == env["memory_id"]
        inputs = list(db.scalars(sa.select(MemoryMaintenanceInput).where(MemoryMaintenanceInput.job_id == row.job_id).order_by(MemoryMaintenanceInput.input_id)))
        manifest = manifest_for_job(row, inputs)
    state["manifest"] = manifest
    if len(manifest["inputs"]) != len(sources) or any(item["coverage"] != "included" or item.get("fragment") is not None for item in manifest["inputs"]):
        state["state"] = "source_coverage_not_comparable"
        return
    assert {item["source_id"] for item in manifest["inputs"]} == {int(source["source_id"]) for source in sources}
    assert {item["entry_id"] for item in manifest["entries"]} == set(request["entry_ids"])
    env["comparison_expected_payload"] = json.dumps(analysis_payload(manifest, case["policy"]), ensure_ascii=False, separators=(",", ":"))
    env["observer"].set_stage("analysis", phase=label + "-analysis")
    with capture_analyst(env, patch):
        response = api_request(env, "POST", f"/memories/{env['memory_id']}/maintenance-jobs/{job['job_id']}/runs", actor=0, json={"operation_id": label + "-run"})
        state["run_response"] = {"http_status": response.status_code, "body": response.json()}
        if response.status_code not in {200, 202} or response.json().get("retcode") != 0:
            state["state"] = "submission_rejected"
            return
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            assert not env["worker_errors"], [type(error).__name__ for error in env["worker_errors"]]
            current = call(env, "GET", f"/memories/{env['memory_id']}/maintenance-jobs/{job['job_id']}")
            state["settled_job"] = current
            execution = current.get("execution") or {}
            if execution.get("state") in {"succeeded", "failed", "canceled", "needs_reconciliation"}:
                state["state"] = "succeeded" if execution["state"] == "succeeded" and current["state"] == "ready" else "analysis_failed"
                candidates = call(env, "GET", f"/memories/{env['memory_id']}/maintenance-jobs/{job['job_id']}/candidates")["candidates"]
                state["candidates"] = [{**item, "job_id": job["job_id"]} for item in candidates]
                return
            time.sleep(0.1)
    state["state"] = "analysis_unsettled"


def validate_adoption(request: dict[str, Any], decision: Any) -> dict[str, int | None]:
    semantic = deepcopy(decision)
    selected: dict[str, int | None] = {}
    if not isinstance(semantic, dict) or not isinstance(semantic.get("variants"), dict):
        raise ValueError("invalid_longitudinal_adoption")
    if set(semantic["variants"]) != set(request["variants"]):
        raise ValueError("invalid_longitudinal_variants")
    for alias, view in semantic["variants"].items():
        if not isinstance(view, dict) or "adopt_candidate_index" not in view or "adoption_reason" not in view:
            raise ValueError("explicit_longitudinal_adoption_required")
        index, reason = view.pop("adopt_candidate_index"), view.pop("adoption_reason")
        if not isinstance(reason, str) or not reason.strip() or (index is not None and (type(index) is not int or index < 0 or index >= len(request["variants"][alias]["candidates"]))):
            raise ValueError("invalid_longitudinal_selection")
        if index is not None and request["variants"][alias]["analysis_state"] != "succeeded":
            raise ValueError("unready_longitudinal_selection")
        selected[alias] = index
    validate_review(request, semantic)
    return selected


def external_adoption(directory: Path, case: dict[str, Any], phase: str, outcomes: dict[Branch, dict[str, Any]], journal: Any) -> tuple[dict[Branch, int | None], dict[str, Any]]:
    sources = [case["initial"], *([case["correction"]] if phase == "corrected" else [])]
    adapter = {"id": case["id"] + "-" + phase, "sources": sources, "rubric": case["rubric"][phase]["candidate"]}
    raw = {"branches": outcomes}
    request, mapping = review_request(adapter, raw)
    for alias, branch in mapping["variants"].items():
        for index, candidate in enumerate(outcomes[branch]["candidates"]):
            request["variants"][alias]["candidates"][index].update(base_content=candidate.get("base_content"), diff=candidate.get("diff"))
    request["longitudinal_phase"] = phase
    request["adoption_instruction"] = (
        "After independent semantic review, explicitly select one candidate index or null per variant and state adoption_reason. No score automatically adopts. Later phases may be inferable from prior version-bound verification; this is not a fully blinded longitudinal trial."
    )
    label = case["id"] + "-" + phase
    path = directory / (label + ".adoption-decision.json")
    request["decision_path"] = str(path)
    request["review_sha256"] = digest({key: value for key, value in request.items() if key != "review_sha256"})
    mapping["review_sha256"] = request["review_sha256"]
    journal.event("reserve-independent-adoption-decision", path=str(path), review_sha256=request["review_sha256"])
    write_private(directory / (label + ".candidate-review-request.json"), request, journal)
    write_private(directory / (label + ".candidate-raw-report.json"), raw, journal)
    side = directory / "blinding"
    if not side.exists():
        journal.event("reserve-longitudinal-blinding-directory", path=str(side))
        side.mkdir(mode=0o700)
    write_private(side / (label + ".mapping.json"), mapping, journal)
    deadline = time.monotonic() + 600
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if not path.is_file():
        raise AssertionError("external_longitudinal_adoption_missing")
    decision = json.loads(path.read_text())
    selected = validate_adoption(request, decision)
    return {branch: selected[alias] for alias, branch in mapping["variants"].items()}, decision


def publish_once(env: dict[str, Any], case: dict[str, Any], branch: Branch, phase: str, candidate: dict[str, Any], entry: dict[str, Any] | None, state: dict[str, Any]) -> dict[str, Any] | None:
    """One existing API attempt; never hide extra embedding calls in reconciliation."""
    expected_action = "patch" if entry is not None else "add"
    if candidate["action"] != expected_action or (entry is not None and candidate["target_entry_id"] != entry["entry_id"]):
        raise ValueError("adopted_candidate_does_not_match_phase")
    label, mid = case["id"] + "-" + branch + "-" + phase, env["memory_id"]
    state["review_operation_id"] = label + "-approve"
    response = api_request(
        env,
        "POST",
        f"/memories/{mid}/maintenance-jobs/{candidate['job_id']}/candidates/{candidate['candidate_id']}/reviews",
        actor=0,
        json={"operation_id": state["review_operation_id"], "expected_revision": candidate["revision"], "decision": "approve"},
    )
    state["review_response"] = {"http_status": response.status_code, "body": response.json()}
    if response.status_code not in {200, 202} or response.json().get("retcode") != 0 or "revision" not in response.json().get("data", {}):
        state["state"] = "candidate_review_unsettled_or_rejected"
        return None
    reviewed = result(response, status=response.status_code)
    env["observer"].set_stage("publication_embedding", phase=label + "-publication")
    response = api_request(
        env,
        "POST",
        f"/memories/{mid}/maintenance-jobs/{candidate['job_id']}/publications",
        actor=0,
        json={"operation_id": label + "-publish", "candidates": [{"candidate_id": candidate["candidate_id"], "expected_revision": reviewed["revision"]}]},
    )
    state["publication_response"] = {"http_status": response.status_code, "body": response.json()}
    if response.status_code not in {200, 202} or response.json().get("retcode") != 0:
        state["state"] = "publication_rejected"
        return None
    operation = result(response, status=response.status_code)
    if operation.get("status") != "ready" or operation.get("index_sync_status") != "ready":
        state["state"] = "publication_unsettled"
        return None
    assert len(operation["entries"]) == 1
    current = call(env, "GET", f"/memories/{mid}/entries/{operation['entries'][0]['entry_id']}")
    state.update(published_entry=current, state="published")
    return current


def startup(env: dict[str, Any], entry_id: str) -> dict[str, Any]:
    before, attempts = fingerprint(env), len(env["observer"].attempts)
    response = api_request(
        env,
        "POST",
        f"/memories/{env['memory_id']}/startup-context",
        actor=0,
        json={"user_id": SUBJECT, "agent_id": AGENT, "session_id": None, "entry_ids": [entry_id], "budget_tokens": 8192, "max_items": 1},
    )
    assert response.headers.get("cache-control") == "no-store"
    packet = result(response)
    assert before == fingerprint(env) and attempts == len(env["observer"].attempts)
    assert packet["usage"]["estimated_tokens"] <= packet["usage"]["budget_tokens"]
    return packet


def query_answer(env: dict[str, Any], case: dict[str, Any], branch: Branch, phase: str, packet: dict[str, Any], state: dict[str, Any], answerer: Answerer) -> None:
    label = case["id"] + "-" + branch + "-" + phase
    env["observer"].set_stage("retrieval_embedding", phase=label + "-query")
    question = case["questions"][phase]
    context = call(
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
            "session_id": None,
            "similarity_threshold": 0,
        },
    )
    assert all(item["memory_id"] == env["memory_id"] and item["user_id"] == SUBJECT and item["agent_id"] == AGENT for item in context["items"]), "cross_bank_or_subject_context"
    state.update(context=context, context_sha256=digest(context), question=question, startup=packet)
    env["observer"].set_stage("answer", phase=label + "-answer")
    state["answer"] = asyncio.run(answerer(env, context, packet, question))


def run_lifecycle(env: dict[str, Any], patch: pytest.MonkeyPatch, case: dict[str, Any], case_index: int, directory: Path, *, answerer: Answerer = answer) -> dict[str, Any]:
    """Candidates stay unverified; external decisions are mandatory at every gate."""
    calls = planned_calls(case)
    env["observer"].max_attempts = sum(calls.values())
    env["comparison_requests"] = []
    report: dict[str, Any] = {
        "case_id": case["id"],
        "synthetic_only": True,
        "plan": calls,
        "branches": {name: {"phases": {}, "active": True, "sources": [], "entry": None} for name in BRANCHES},
        "adoption_decisions": {},
        "analyst_requests": env["comparison_requests"],
        "mechanism_complete": False,
        "semantic_review_complete": False,
        "blinding_limit": "Initial candidate review is anonymous; later phases may be inferable from verification snapshots. Answer review is not blinded.",
        "no_automatic_approval": True,
    }
    started = time.monotonic()
    try:
        with peer_banks(env) as banks:
            report["bank_mapping"] = {name: bank["memory_id"] for name, bank in banks.items()}
            for bank in banks.values():
                bank["comparison_requests"] = env["comparison_requests"]
            for phase in ("initial", "corrected"):
                if phase == "corrected" and case["correction"] is None:
                    continue
                order = branch_order(case_index) if phase == "initial" else tuple(reversed(branch_order(case_index)))
                phase_outcomes: dict[Branch, dict[str, Any]] = {}
                for name in order:
                    progress = report["branches"][name]
                    state: dict[str, Any] = {"state": "not_run", "sources": list(progress["sources"]), "candidates": []}
                    phase_outcomes[name] = state
                    progress["phases"][phase] = state
                    if not progress["active"]:
                        state["state"] = "skipped_after_decline_or_failure"
                        continue
                    source = append_source(banks[name], patch, case, name, phase, state)
                    if source is None:
                        progress["active"] = False
                        continue
                    progress["sources"].append(source)
                    analyze_sources(banks[name], patch, case, name, phase, progress["sources"], progress["entry"], state)
                if all(phase_outcomes[name].get("source") is not None for name in BRANCHES):
                    verify_source_pair(case["initial" if phase == "initial" else "correction"], phase_outcomes["legacy"]["source"], phase_outcomes["structured"]["source"])
                if not any(item["state"] == "succeeded" for item in phase_outcomes.values()):
                    for name in BRANCHES:
                        report["branches"][name]["active"] = False
                    continue
                selected, decision = external_adoption(directory, case, phase, phase_outcomes, env["journal"])
                report["adoption_decisions"][phase] = decision
                for name in order:
                    progress, state = report["branches"][name], phase_outcomes[name]
                    choice = selected[name]
                    if choice is None:
                        state["adoption"] = "explicitly_declined_or_unavailable"
                        if state["state"] == "succeeded":
                            state["state"] = "explicitly_declined"
                        progress["active"] = False
                        continue
                    current = publish_once(banks[name], case, name, phase, state["candidates"][choice], progress["entry"], state)
                    if current is None:
                        progress["active"] = False
                        continue
                    progress["entry"] = current
                    unconfirmed = startup(banks[name], current["entry_id"])
                    assert unconfirmed["items"] == [], "Publication or revision alone cannot substitute for current human confirmation"
                    state["startup_before_confirmation"] = unconfirmed
                    state["confirmation"] = judgment(
                        banks[name], current["entry_id"], "human_confirmed", case["id"] + "-" + name + "-" + phase + "-confirm", case["applicability"], directory, review_timeout_seconds=600
                    )
                    packet = startup(banks[name], current["entry_id"])
                    assert [item["entry_id"] for item in packet["items"]] == [current["entry_id"]]
                    assert packet["items"][0]["assessment"]["verification_status"] == "unverified"
                    query_answer(banks[name], case, name, phase, packet, state, answerer)
                    state["state"] = "answered"
            for name in branch_order(case_index):
                progress = report["branches"][name]
                if not progress["active"]:
                    continue
                entry = progress["entry"]
                assert entry is not None
                identity = progress["sources"][0]["source_id"]
                current = call(banks[name], "GET", f"/memories/{banks[name]['memory_id']}/sources/{identity}/state")
                state = {"state": "withdrawing"}
                progress["phases"]["withdrawn"] = state
                state["withdrawal"] = call(
                    banks[name],
                    "POST",
                    f"/memories/{banks[name]['memory_id']}/sources/{identity}/withdrawals",
                    json={
                        "operation_id": case["id"] + "-" + name + "-withdraw",
                        "expected_source_revision": int(current["source_revision"]),
                        "reason": "Owned synthetic source replay; stop supply after withdrawal.",
                    },
                )
                assert state["withdrawal"]["stop_supply"] == "withdrawn"
                packet = startup(banks[name], entry["entry_id"])
                assert packet["items"] == []
                query_answer(banks[name], case, name, "withdrawn", packet, state, answerer)
                # Surviving independent sources are retained in the query context.
                forbidden_ids = {str(entry["current_message_id"]), str(identity)}
                assert all(str(item["message_id"]) not in forbidden_ids for item in state["context"]["items"])
                state["state"] = "answered"
            report["mechanism_complete"] = all(item["active"] and item["phases"].get("withdrawn", {}).get("state") == "answered" for item in report["branches"].values())
    except BaseException as error:
        report["error_code"], report["failed_phase"] = type(error).__name__, env["observer"].phase
        raise
    finally:
        report["elapsed_seconds_including_external_review"] = time.monotonic() - started
        report["provider_attempts"] = env["observer"].attempts
        report["cost"] = costs(env)
        write_private(directory / (case["id"] + ".longitudinal-report.json"), report, env["journal"])
        review = {
            "case_id": case["id"],
            "synthetic_only": True,
            "answer_review_blinded": False,
            "report_sha256": digest(report),
            "rubric": {phase: criteria["answer"] for phase, criteria in case["rubric"].items() if criteria is not None},
            "branches": {
                name: {
                    phase: {"question": item.get("question"), "actual_context": item.get("context"), "actual_startup": item.get("startup"), "answer": item.get("answer")}
                    for phase, item in branch["phases"].items()
                    if item.get("answer") is not None
                }
                for name, branch in report["branches"].items()
            },
            "instruction": "Independently compare every answer with the actual current inputs, including legitimate surviving sources after withdrawal. Do not equate human confirmation with objective truth or infer unseen evidence. This answer review is not blinded after prior verification steps.",
        }
        review["review_sha256"] = digest(review)
        write_private(directory / (case["id"] + ".answer-review-request.json"), review, env["journal"])
    return report
