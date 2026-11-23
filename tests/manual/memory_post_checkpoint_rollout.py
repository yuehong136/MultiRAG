"""Exercise an independently sealed withdrawal and retirement on a restored stack."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.manual.memory_same_point_rollout import ROOT, Capture, RecoveryPointMismatch, _child_environment, admit_restored, archive_hash, check_archives, fingerprint, snapshots
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import private_consumer_file


def _read_tail(path: Path, expected: str, bindings: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if archive_hash(path) != expected:
        raise RecoveryPointMismatch("post-checkpoint receipt digest mismatch")
    tail = json.loads(path.read_text())
    if tail["state_a"] != bindings["pointA"] or tail["state_b"] != bindings["pointB"]:
        raise RecoveryPointMismatch("post-checkpoint receipt checkpoint mismatch")
    if tail["actor_id"] != bindings["actorId"] or tail["memory_id"] != bindings["memoryId"]:
        raise RecoveryPointMismatch("post-checkpoint receipt scope mismatch")
    if tail["high_water"] != 2 or [operation["sequence"] for operation in tail["operations"]] != [1, 2]:
        raise RecoveryPointMismatch("post-checkpoint receipt incomplete")
    retirement, withdrawal = tail["operations"]
    if (retirement["artifact_id"], retirement["operation_id"], withdrawal["source_id"]) != (
        bindings["retiredArtifactId"],
        bindings["retiredOperationId"],
        bindings["withdrawnSourceId"],
    ):
        raise RecoveryPointMismatch("post-checkpoint receipt target mismatch")
    return retirement, withdrawal


def _data(response: httpx.Response) -> Any:
    assert response.status_code == 200 and response.json()["retcode"] == 0, (response.status_code, response.json()["retcode"])
    return response.json()["data"]


async def _apply_tail(client: httpx.AsyncClient, bindings: dict[str, Any], retirement: dict[str, Any], withdrawal: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    base = f"/api/v1/memories/{bindings['memoryId']}"
    source = f"{base}/sources/{withdrawal['source_id']}"
    artifact = f"{base}/context-compactions/{retirement['artifact_id']}"
    preview = _data(await client.get(artifact + "/retention-preview"))
    current = _data(await client.get(source + "/state"))
    if not preview["eligible"] or preview["operation_id"] != retirement["operation_id"] or preview["preview_sha256"] != retirement["preview_sha256"]:
        raise RecoveryPointMismatch("post-checkpoint retention preview changed")
    if current != withdrawal["source_before"] or withdrawal["request"]["expected_source_revision"] != int(current["source_revision"]):
        raise RecoveryPointMismatch("post-checkpoint source version changed")
    assert _data(await client.get(source))
    assert _data(await client.get(f"{base}/context-compaction-operations/{bindings['dependentOperationId']}"))["artifact"]["package"]
    entry = f"{base}/entries/{bindings['dependentEntryId']}"
    assert _data(await client.get(entry))["current"]["content"] == "Synthetic source-dependent color rule."
    retire_body = {"expected_preview_sha256": preview["preview_sha256"]}
    withdraw_body = withdrawal["request"]
    retired = _data(await client.post(artifact + "/retirements", json=retire_body))
    assert retired["artifact"]["error_code"] == "compaction_retired" and retired["artifact"]["package"] is None
    applied = _data(await client.post(source + "/withdrawals", json=withdraw_body))
    assert applied["operation_id"] == withdraw_body["operation_id"] and applied["index_sync_status"] == "ready"
    current = _data(await client.get(source + "/state"))
    for key in ("source_id", "source_revision", "generation", "state", "content_sha256"):
        assert current[key] == withdrawal["source_after"][key], key
    assert current["state"] == "forgotten" and (await client.get(source)).status_code == 404
    dependent = _data(await client.get(f"{base}/context-compaction-operations/{bindings['dependentOperationId']}"))
    assert dependent["artifact"]["package"] is None
    current_entry = _data(await client.get(entry))["current"]
    assert current_entry["redacted"] is True and current_entry["content"] is None
    readback = _data(await client.get(f"{base}/operations/{withdraw_body['operation_id']}"))
    # GET additionally attaches recovery and ownership metadata.
    assert {field: readback[field] for field in applied} == applied
    return retired, applied


def _target(work: Path) -> None:
    assert work == Path(os.environ["TMPDIR"]).resolve() / "memory-post-checkpoint"
    assert Path(os.environ["MULTIRAG_CONFIG_OVERLAY_FILE"]) == work / "target.json"
    from common.app_config import get_app_config
    from common.bootstrap import ensure_initialized
    from common.config_utils import CONFIGS

    bindings = json.loads((work / "bindings.json").read_text())
    pg = CONFIGS["postgresql"]
    assert [pg["host"], int(pg["port"]), pg["dbname"]] == bindings["owned_postgresql"]
    for service, endpoint in bindings["owned_hosts"].items():
        assert CONFIGS[service]["hosts" if service == "milvus" else "host"] == endpoint
    secret = get_app_config().multirag.secret_key
    assert secret is not None and secret.get_secret_value() == os.environ["MULTIRAG_MULTIRAG__SECRET_KEY"]
    ensure_initialized()

    from api.apps import app
    from api.db.db_models import engine
    from api.db.services.tenant_llm_service import TenantLLMService
    from tests.manual.memory_same_point_web import smoke_target

    assert [engine.url.host, engine.url.port, engine.url.database] == bindings["owned_postgresql"]
    journal = ResourceJournal("memory-post-checkpoint-target")
    before = snapshots(engine, bindings)
    admit_restored(before, bindings["pointA"])
    tail_path = work / "tail.json"
    expected = bindings["tail_sha256"]
    for path, digest, error in (
        (work / "absent.json", expected, "backup archive missing"),
        (tail_path, "0" * 64, "receipt digest mismatch"),
        (work / "incomplete.json", bindings["incomplete_sha256"], "receipt incomplete"),
    ):
        with pytest.raises(RecoveryPointMismatch, match=error):
            _read_tail(path, digest, bindings)
        assert snapshots(engine, bindings) == before
        journal.event("post-checkpoint-rejected", cause=error, listener_started=False, worker_started=False, storage_unchanged=True)
    retirement, withdrawal = _read_tail(tail_path, expected, bindings)
    model_attempts = 0

    def forbid_model(*args: Any, **kwargs: Any) -> None:
        nonlocal model_attempts
        model_attempts += 1
        raise AssertionError("Post-checkpoint reconciliation may not dispatch a model")

    def recovery_client() -> httpx.AsyncClient:
        # Only the rehearsal owns this private in-process client before admission.
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Authorization": "Bearer " + bindings["token"]})

    async def check_drift() -> None:
        async with recovery_client() as client:
            with pytest.raises(RecoveryPointMismatch, match="retention preview changed"):
                await _apply_tail(client, bindings, {**retirement, "preview_sha256": "0" * 64}, withdrawal)
            with pytest.raises(RecoveryPointMismatch, match="source version changed"):
                await _apply_tail(client, bindings, retirement, {**withdrawal, "source_before": {}})

    async def check_recovery() -> tuple[dict[str, Any], dict[str, Any]]:
        async with recovery_client() as client:
            return await _apply_tail(client, bindings, retirement, withdrawal)

    async def check_duplicate(retired: dict[str, Any], withdrawn: dict[str, Any]) -> None:
        base = f"/api/v1/memories/{bindings['memoryId']}"
        async with recovery_client() as client:
            assert _data(await client.post(f"{base}/context-compactions/{retirement['artifact_id']}/retirements", json={"expected_preview_sha256": retirement["preview_sha256"]})) == retired
            assert _data(await client.post(f"{base}/sources/{withdrawal['source_id']}/withdrawals", json=withdrawal["request"])) == withdrawn

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(TenantLLMService, "model_instance_from_config", classmethod(forbid_model))
        asyncio.run(check_drift())
        assert snapshots(engine, bindings) == before
        journal.event("post-checkpoint-rejected", cause="source and preview drift", listener_started=False, worker_started=False, storage_unchanged=True)
        retired, withdrawn = asyncio.run(check_recovery())
        after = snapshots(engine, bindings)
        assert after["postgresql"] != before["postgresql"] and after["milvus"] != before["milvus"] and after["redis"] == before["redis"]
        asyncio.run(check_duplicate(retired, withdrawn))
        assert snapshots(engine, bindings) == after
        assert model_attempts == 0
        journal.event(
            "post-checkpoint-admitted",
            receipt_sha256=expected,
            high_water=2,
            original_operations_preserved=True,
            duplicate_storage_unchanged=True,
            dependent_entry_redacted=True,
            model_attempts=model_attempts,
            state_sha256=after,
        )
        smoke_target(engine, bindings)
        assert snapshots(engine, bindings) == after


def _run(work: Path, *, hard_crash: bool) -> None:
    from tests.manual.memory_restore_stores import isolated_stores

    journal = ResourceJournal("memory-post-checkpoint")
    journal.event("reserve-post-checkpoint-work", path=str(work))
    work.mkdir(mode=0o700)
    try:
        base = yaml.safe_load((ROOT / "configs/service_conf.yaml").read_text())
        with isolated_stores(work, journal, name="tail-source") as source:
            config = work / "source.json"
            private_consumer_file(config, json.dumps(source.overlay(base)), journal, [])
            capture = Capture(source, work, journal, {}, {})
            env = dict(os.environ)
            source_env = _child_environment(config)
            try:
                os.environ.clear()
                os.environ.update(source_env)
                args = ["-o", "addopts=", "--allow-unix-socket", "--allow-hosts=localhost,127.0.0.1,::1", "--tb=short", "-q"]
                case = "test_memory_compaction_hard_crash.py" if hard_crash else "test_memory_post_checkpoint_seed.py"
                assert pytest.main([*args, str(ROOT / "tests/manual" / case)], plugins=[capture]) == pytest.ExitCode.OK
            finally:
                os.environ.clear()
                os.environ.update(env)
                config.unlink(missing_ok=True)
            if hard_crash:
                return
            tape = capture.bindings.pop("tail")
            private_consumer_file(work / "tail.json", json.dumps(tape), journal, [])
            private_consumer_file(work / "incomplete.json", json.dumps({**tape, "operations": tape["operations"][:1]}), journal, [])
            expected = archive_hash(work / "tail.json")
            journal.event(
                "post-checkpoint-receipt-sealed", sha256=expected, expected_high_water=tape["high_water"], from_state=fingerprint(tape["state_a"]), through_state=fingerprint(tape["state_b"])
            )
        a, b = capture.points["point-a"], capture.points["point-b"]
        check_archives(a.archives, a.archive_hashes)
        with isolated_stores(work, journal, name="tail-target") as target:
            target.restore(a.archives, capture.bindings["collections"])
            target_config = target.overlay(base)
            overlay = work / "target.json"
            private_consumer_file(overlay, json.dumps(target_config), journal, [])
            url = target.postgres_url
            bindings = {
                **capture.bindings,
                "pointA": a.state,
                "pointB": b.state,
                "tail_sha256": expected,
                "incomplete_sha256": archive_hash(work / "incomplete.json"),
                "owned_postgresql": [url.host, url.port, url.database],
                "owned_hosts": {key: target_config[key]["hosts" if key == "milvus" else "host"] for key in ("milvus", "minio", "redis", "datav_redis")},
            }
            private_consumer_file(work / "bindings.json", json.dumps(bindings), journal, [])
            subprocess.run(
                [sys.executable, "-B", "-m", "tests.manual.memory_post_checkpoint_rollout", "--target"],
                cwd=ROOT,
                env=_child_environment(overlay),
                check=True,
                timeout=200,
            )
    finally:
        shutil.rmtree(work)
        assert not work.exists()
        journal.event("post-checkpoint-work-cleaned", path=str(work), absent=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--target", action="store_true")
    mode.add_argument("--hard-crash", action="store_true")
    args = parser.parse_args()
    if not __debug__ or not os.environ.get("MULTIRAG_RUN_ID") or not os.environ.get("MULTIRAG_RUN_JOURNAL"):
        parser.error("Use scripts/run_registered.py with assertions enabled")
    work = Path(os.environ["TMPDIR"]).resolve() / "memory-post-checkpoint"
    if args.target:
        _target(work)
    else:
        os.environ["MULTIRAG_SECRET_KEY"] = os.environ["MULTIRAG_MULTIRAG__SECRET_KEY"] = secrets.token_hex(32)

        def cancel(signum: int, _frame: Any) -> None:
            raise SystemExit(128 + signum)

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, cancel)
        _run(work, hard_crash=args.hard_crash)


if __name__ == "__main__":
    main()
