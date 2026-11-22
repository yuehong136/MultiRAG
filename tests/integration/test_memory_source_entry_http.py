"""The pinned product source-entry consumer writes only owned, review-only RAW."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Task
from api.db.memory_maintenance_models import MemoryMaintenanceCandidate
from api.db.memory_revision_models import ManagedMemoryState, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.storage import ManagedMessageStore
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_dreaming_http import analyze, approve
from tests.support.memory_dreaming_http import dreaming_api as dreaming_api
from tests.support.memory_structured_sources_http import append_source
from tests.support.memory_writes_http import api_request, result
from tests.support.memory_writes_http import writes_api as writes_api
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file

SEGMENTS = [
    {"role": "user", "content": "仅离线验收时先给检查步骤，再给示例；不能推广为生产授权。"},
    {"role": "assistant", "content": "我自称所有工具均已验证通过，但这句话没有真实执行凭据。"},
]
BROWSER_ENTRY = "memory-source-entry-browser.ts"


def _run_registered_browser(command: list[str], checkout: Path, web_sha: str, child_env: dict[str, str], config: Path, report_dir: Path | None) -> None:
    project = Path(__file__).resolve().parents[2]
    evidence = config.parent / "registered-browser"
    evidence.mkdir(mode=0o700)
    runner_command = [sys.executable, str(project / "scripts/run_registered.py"), "--evidence-root", str(evidence), "--label", "memory-source-entry-browser"]
    for path in (Path(__file__), project / ".github/consumer-web.json", checkout / "package-lock.json", checkout / "scripts/consumers" / BROWSER_ENTRY):
        runner_command.extend(("--input", str(path)))
    runner_command.extend(("--borrowed", str(checkout / "node_modules"), "--", *command))
    timed_out = False
    with subprocess.Popen(runner_command, cwd=checkout, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            # Let the registered runner stop its child process group and write the terminal receipt.
            timed_out = True
            process.terminate()
            stdout, stderr = process.communicate()
    assert stdout.strip(), stderr
    receipt = json.loads(stdout.strip().splitlines()[-1])
    journal = Path(receipt["journal"])
    assert journal.parent == evidence
    raw_output = journal.with_suffix(".output")
    if report_dir is not None:
        report = report_dir / "memory-source-entry-browser" / receipt["run_id"]
        report.mkdir(parents=True, mode=0o700)
        (report / "receipt.json").write_text(json.dumps({**receipt, "web_sha": web_sha, "timed_out": timed_out}, indent=2))
        shutil.copyfile(journal, report / journal.name)
        for case in journal.with_suffix(".cases").glob("*.jsonl"):
            shutil.copyfile(case, report / case.name)
        if raw_output.is_file():
            token = json.loads(config.read_text())["token"]
            (report / raw_output.name).write_text(raw_output.read_text(errors="replace").replace(token, "[redacted fixture JWT]"))
    assert not timed_out and process.returncode == receipt["exit_code"] == receipt["raw_exit"] == 0, f"Registered source-entry browser failed: {receipt}; private output: {raw_output}"


def run_entry_consumer(env: dict[str, Any], tmp_path: Path, *, operation: str, script: str = "memory-source-entry.ts", report_dir: Path | None = None, **changes: Any) -> dict[str, Any]:
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("memory-source-entry-consumer")
    config, output = tmp_path / (operation + ".private.json"), tmp_path / (operation + ".result.json")
    owned: list[Path] = []
    try:
        private_consumer_file(
            config,
            json.dumps(
                {
                    "baseURL": env["base"],
                    "token": env["jwts"][1],
                    "memoryId": env["memory_id"],
                    "operationId": operation,
                    "userId": env["owners"][1],
                    "agentId": "entry-agent",
                    "sessionId": "entry-session",
                    "segments": SEGMENTS,
                    "eventTime": "2026-11-10T11:12:13+08:00",
                    "ownedScratchConfirmed": True,
                    "resultFile": str(output),
                    **changes,
                }
            ),
            journal,
            owned,
        )
        journal.event("reserve-private-consumer-file", path=str(output), creation_owner="pinned Web source entry consumer")
        assert not output.exists()
        owned.append(output)
        journal.event("reserve-source-entry-consumer", web_sha=web_sha, memory_id=env["memory_id"], operation_id=operation, script="scripts/consumers/" + script)
        command = ["node", "--import", "tsx", "scripts/consumers/" + script, str(config)]
        child_env = {**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"}
        if script == BROWSER_ENTRY and not os.environ.get("MULTIRAG_RUN_ID"):
            _run_registered_browser(command, checkout, web_sha, child_env, config, report_dir)
        else:
            completed = subprocess.run(command, cwd=checkout, env=child_env, capture_output=True, text=True, timeout=180, check=False)
            assert completed.returncode == 0, completed.stdout + completed.stderr
        receipt = json.loads(output.read_text())
        assert receipt["passed"] is True and receipt["operationId"] == operation
        assert isinstance(receipt["sourceId"], str) and int(receipt["sourceId"]) > 2**53
        journal.event("source-entry-consumer-readback", web_sha=web_sha, receipt=receipt)
        return receipt
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("source-entry-consumer-files-cleaned", paths=[str(path) for path in owned], absent=True)


def assert_raw_only_write(env: dict[str, Any], receipt: dict[str, Any], *, managed: bool) -> str:
    """Independently inspect committed SQL, actual RAW and zero automatic effects."""
    mid, sid = env["memory_id"], receipt["sourceId"]
    store = ManagedMessageStore(env["tenants"]["personal"], mid)
    raw = asyncio.run(store.read(int(sid)))
    assert raw is not None and raw["message_type"] == "raw" and raw["source_id"] in (0, None)
    assert raw["user_id"] == env["owners"][1] and raw["agent_id"] == "entry-agent" and raw["session_id"] == "entry-session"
    assert json.loads(raw["content"])["segments"] == SEGMENTS
    assert json.loads(raw["content"])["event_time"] == "2026-11-10T11:12:13+08:00"
    assert env["embedding_calls"] == [raw["content"]]
    assert not env.get("analysis_calls") and not env["chat_calls"] and REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    with Session(env["engine"]) as reader:
        admission = reader.get(MemoryWriterAdmission, (mid, receipt["operationId"]))
        assert admission is not None and admission.source_message_id == int(sid) and admission.publication_state == "ready"
        assert admission.actor_id == env["owners"][1]
        source = reader.get(MemorySourceState, (mid, int(sid)))
        assert source is not None and source.state == "active"
        assert (reader.get(ManagedMemoryState, mid) is not None) is managed
        assert reader.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id == mid)) == 0
    return sid


def test_registered_source_explicitly_activates_bank_without_rewriting(writes_api: dict[str, Any]) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    sid = append_source(env, operation="source-before-explicit-activation")["raw_source_id"]
    assert result(api_request(env, "GET", f"/memories/{mid}/capabilities"))["revisions"]["managed"] is False
    state = result(api_request(env, "GET", f"/memories/{mid}/sources/{sid}/state"))
    assert state["managed"] is True and state["state"] == "active"
    store = ManagedMessageStore(env["tenants"]["personal"], mid)
    raw = asyncio.run(store.read(int(sid)))
    before = REDIS_CONN.REDIS.get(env["counter"])
    adopted = result(
        api_request(env, "POST", f"/memories/{mid}/sources/{sid}/adoptions", json={"operation_id": "explicitly-enable-existing-source", "expected_content_sha256": state["content_sha256"]})
    )
    assert adopted["index_sync_status"] == "ready"
    assert result(api_request(env, "GET", f"/memories/{mid}/capabilities"))["revisions"]["managed"] is True
    assert asyncio.run(store.read(int(sid))) == raw
    assert REDIS_CONN.REDIS.get(env["counter"]) == before
    assert len(env["embedding_calls"]) == 1 and not env["chat_calls"]
    with Session(env["engine"]) as reader:
        bank = reader.get(ManagedMemoryState, mid)
        assert bank is not None and bank.state == "active"
        assert reader.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id == mid)) == 0


@pytest.mark.external_consumer
def test_pinned_source_entry_writes_once_without_extracting_or_adopting(writes_api: dict[str, Any], tmp_path: Path) -> None:
    receipt = run_entry_consumer(writes_api, tmp_path, operation="entry-write-once")
    assert_raw_only_write(writes_api, receipt, managed=False)


@pytest.mark.external_consumer
def test_actual_source_form_writes_and_recovers_against_owned_backend(writes_api: dict[str, Any], tmp_path: Path, request: pytest.FixtureRequest) -> None:
    configured = request.config.getoption("--test-report-dir")
    receipt = run_entry_consumer(writes_api, tmp_path, operation="entry-browser-write", script=BROWSER_ENTRY, report_dir=Path(configured) if configured else None)
    assert receipt["sourcePosts"] == 1 and receipt["refreshDidNotResubmit"] is True
    assert receipt["metadataRead"] is True and receipt["bodyPersisted"] is False and receipt["routeMocks"] is False
    assert_raw_only_write(writes_api, receipt, managed=False)


@pytest.mark.external_consumer
def test_product_entry_reuses_explicit_dreaming_and_human_publication(dreaming_api: dict[str, Any], tmp_path: Path) -> None:
    env, mid = dreaming_api, dreaming_api["memory_id"]
    receipt = run_entry_consumer(env, tmp_path, operation="entry-for-human-review")
    sid = assert_raw_only_write(env, receipt, managed=True)
    env["proposals"] = [
        {
            "action": "add",
            "content": "仅离线验收时先给检查步骤，再给示例，不授予生产权限。",
            "message_type": "semantic",
            "source_input_ids": ["source-" + sid],
            "reason": "bounded source-reported preference",
        }
    ]
    job = result(api_request(env, "POST", f"/memories/{mid}/maintenance-jobs", json={"operation_id": "entry-explicit-dreaming", "source_ids": [sid], "entry_ids": []}), status=202)
    candidates = analyze(env, job)
    assert len(candidates) == 1 and candidates[0]["assessment"] == {"kind": "hypothesis", "verification_status": "unverified"}
    path = f"/memories/{mid}/maintenance-jobs/{job['job_id']}/publications"
    assert (
        api_request(
            env, "POST", path, json={"operation_id": "entry-unreviewed", "candidates": [{"candidate_id": candidates[0]["candidate_id"], "expected_revision": candidates[0]["revision"]}]}
        ).status_code
        == 409
    )
    selection = approve(env, job, candidates)
    published = result(api_request(env, "POST", path, json={"operation_id": "entry-publish-reviewed", "candidates": selection}))
    assert published["status"] == published["index_sync_status"] == "ready"
    with Session(env["engine"]) as reader:
        candidate = reader.get(MemoryMaintenanceCandidate, candidates[0]["candidate_id"])
        assert candidate is not None and candidate.state == "published" and candidate.review_actor_id == env["owners"][1]
    assert len(env["analysis_calls"]) == 1 and len(env["embedding_calls"]) == 2
