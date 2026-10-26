"""Actual pinned Web model clients, authenticated HTTP and independent SQL."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.support.resource_journal import ResourceJournal
from tests.support.rest_document_contract import rest_documents as rest_documents
from tests.support.rest_document_contract import snapshot

pytestmark = pytest.mark.external_consumer
ROOT = Path(__file__).resolve().parents[2]


def _private_file(path: Path, value: str, journal: ResourceJournal, owned: list[Path]) -> None:
    journal.event("reserve-private-consumer-file", path=str(path), content_sha256=hashlib.sha256(value.encode()).hexdigest())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    owned.append(path)
    try:
        with os.fdopen(descriptor, "w", closefd=False) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _consumer(checkout: Path, descriptor: Path, phase: str, evidence: Path, journal: ResourceJournal, script_name: str = "verify-provider-instance-routes.ts") -> dict[str, Any]:
    runner = Path(os.environ.get("MULTIRAG_CANONICAL_RUNNER", str(ROOT / "scripts/run_registered.py")))
    node = shutil.which("node")
    assert node is not None, "The pinned Web consumer requires Node.js"
    script = checkout / "scripts" / script_name
    assert script.is_file(), "The pinned Web provider-instance consumer is required"
    inputs = [runner, runner.with_name("resource_journal.py"), Path(__file__), descriptor, script, Path(node), ROOT / ".github/consumer-web.json"]
    configuration = json.loads(descriptor.read_text())
    inputs.extend(Path(configuration[key]) for key in ("ownerTokenFile", "foreignTokenFile", "providerKeyFile", "readbackFile") if key in configuration and Path(configuration[key]).is_file())
    inputs.extend(path for path in (checkout / "src").rglob("*") if path.is_file() and path.suffix in {".ts", ".tsx", ".json"})
    inputs.extend(checkout / name for name in ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.json", "tsconfig.app.json") if (checkout / name).is_file())
    for package in ("tsx", "esbuild", "@esbuild", "get-tsconfig", "resolve-pkg-maps"):
        inputs.extend(path for path in (checkout / "node_modules" / package).resolve().rglob("*") if path.is_file())
    for package in (checkout / "node_modules/@esbuild").iterdir():
        inputs.extend(path for path in package.resolve().rglob("*") if path.is_file())
    for key in ("MULTIRAG_CONFIG_OVERLAY_FILE",):
        if value := os.environ.get(key):
            inputs.append(Path(value))
    command = [sys.executable, "-B", str(runner), "--evidence-root", str(evidence), "--label", "provider-web-" + phase]
    for path in dict.fromkeys(inputs):
        command.extend(("--input", str(path)))
    command.extend(("--borrowed", str(checkout / "node_modules"), "--", node, "--import", "tsx", str(script)))
    # Using Node's loader avoids the CLI's unnecessary Unix IPC listener and its
    # path-length limit inside long, registered temporary directories.
    env = {**os.environ, "I_WEB_ACCEPTANCE_DESCRIPTOR": str(descriptor), "I_WEB_ACCEPTANCE_MODE": phase, "TSX_TSCONFIG_PATH": str(checkout / "tsconfig.app.json")}
    journal.event("reserve-consumer-command", phase=phase, command=command, cwd=str(checkout), descriptor_sha256=hashlib.sha256(descriptor.read_bytes()).hexdigest())
    process = subprocess.Popen(command, cwd=checkout, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    previous: dict[signal.Signals, Any] = {}

    def cancelled(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"Web consumer interrupted by signal {signum}")

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, cancelled)
        journal.event("consumer-runner-started", phase=phase, pid=process.pid)
        stdout, stderr = process.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        pytest.fail(f"Web consumer timed out: {phase}")
    finally:
        # The nested gate owns a separate child group. Always ask that gate to
        # close and join its group, including journal failures and cancellation.
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        try:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    journal.event("consumer-runner-cleanup-failed", phase=phase, pid=process.pid, reason="canonical gate did not finish; preserve its owned resources")
                    raise
            journal.event("consumer-runner-joined", phase=phase, pid=process.pid, exit_code=process.returncode)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    assert process.returncode == 0, stdout + stderr
    receipt = json.loads(stdout.strip().splitlines()[-1])
    assert receipt["raw_exit"] == 0 and receipt["exit_code"] == 0
    output = Path(receipt["journal"]).with_suffix(".output")
    result = json.loads(output.read_text().strip().splitlines()[-1])
    assert result["mode"] == phase and result["status"] == "passed", result
    journal.event("consumer-result", phase=phase, receipt=receipt, result=result)
    return result


def _pinned_checkout() -> tuple[Path, str]:
    checkout = Path(os.environ["WEB_COMPLETION_CHECKOUT"])
    web_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    pin = json.loads((ROOT / ".github/consumer-web.json").read_text())["ref"]
    assert web_sha == pin, "Actual Web checkout must equal the consumer pin"
    consumer_status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", "src", "scripts", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.json", "tsconfig.app.json"],
        cwd=checkout,
        text=True,
    )
    assert not consumer_status, "Pinned Web consumer source/configuration must be clean: " + consumer_status
    return checkout, web_sha


def test_actual_web_provider_instances_and_foreign_denials(rest_documents: dict[str, Any], tmp_path: Path, record_property: Callable[[str, object], None]) -> None:
    from api.db.db_models import Tenant, TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider
    from api.db.joint_services.tenant_model_service import get_model_config_by_id

    env = rest_documents
    journal = env["journal"]
    checkout, web_sha = _pinned_checkout()
    private = tmp_path / "provider-web-private"
    evidence = journal.path.parent / (journal.run_id + "-web-consumer")
    journal.event("reserve-consumer-directories", private=str(private), evidence=str(evidence))
    private.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)
    descriptor_path = Path(os.environ.get("I_WEB_ACCEPTANCE_DESCRIPTOR_PATH", str(private / "descriptor.json")))
    files = [private / "owner.token", private / "foreign.token", private / "provider.keys.json", private / "readback.json", descriptor_path]
    keys = [
        json.dumps({"api_key": "controlled-provider-one", "is_tools": True, "opaque": {"instance": 1}}),
        json.dumps({"api_key": "controlled-provider-two", "is_tools": False, "opaque": {"instance": 2}}),
    ]
    descriptor = {
        "origin": env["base"],
        "ownerTokenFile": str(files[0]),
        "foreignTokenFile": str(files[1]),
        "providerKeyFile": str(files[2]),
        "owners": {"ownerId": env["owners"][0], "foreignId": env["owners"][1]},
        "providerName": "OpenAI-API-Compatible",
        "instanceNames": ["web-first-" + journal.run_id[:12], "web-second-" + journal.run_id[:12]],
        "modelName": "web-model-" + journal.run_id[:12],
        "scratchDatabase": env["engine"].url.database,
        "readbackFile": str(files[3]),
    }
    journal.event(
        "reserve-web-model-resources",
        database=descriptor["scratchDatabase"],
        owners=env["owners"],
        provider=descriptor["providerName"],
        instances=descriptor["instanceNames"],
        model=descriptor["modelName"],
        private_paths=[str(path) for path in files],
    )
    owned_files: list[Path] = []
    try:
        _private_file(files[0], env["jwt"][0], journal, owned_files)
        _private_file(files[1], env["jwt"][1], journal, owned_files)
        _private_file(files[2], json.dumps(keys), journal, owned_files)
        _private_file(descriptor_path, json.dumps(descriptor), journal, owned_files)
        result = _consumer(checkout, descriptor_path, "owner", evidence, journal)
        _private_file(files[3], json.dumps(result), journal, owned_files)
        identifier = result["modelIds"]["surviving"]
        deleted_id = result["modelIds"]["deleted"]
        owner_id = env["owners"][0]
        with Session(env["engine"]) as db:
            provider = db.scalars(select(TenantModelProvider).where(TenantModelProvider.tenant_id == owner_id)).one()
            assert provider.provider_name == descriptor["providerName"]
            instance = db.scalars(select(TenantModelInstance).where(TenantModelInstance.provider_id == provider.id)).one()
            assert instance.instance_name == descriptor["instanceNames"][0]
            assert instance.api_key == keys[0] and instance.base_url == "https://w-edited.invalid" and instance.region == ""
            assert instance.status == "active"
            row = db.get(TenantLLM, identifier)
            model = db.get(TenantModel, identifier)
            assert row is not None and model is not None
            assert row.tenant_id == owner_id and row.max_tokens == 0 and row.status == "1"
            assert model.instance_id == instance.id and model.model_name == descriptor["modelName"]
            assert set(model.model_types) == {"chat", "image2text"} and model.is_tools is False
            assert db.get(TenantModel, deleted_id) is None and db.get(TenantLLM, deleted_id) is None
            tenant = db.get(Tenant, owner_id)
            assert tenant is not None and tenant.tenant_llm_id == identifier and tenant.llm_id == str(identifier)
            assert tenant.tenant_ocr_id is None and not tenant.ocr_id
            for capability in ("chat", "image2text"):
                config = get_model_config_by_id(db, identifier, tenant_id=owner_id, model_type=capability)
                assert config["api_base"] == instance.base_url and config["mdl_type"] == capability
                assert config["api_key"] == "controlled-provider-one" and config["is_tools"] is False
                assert json.loads(config["api_key_payload"])["opaque"] == {"instance": 1}
        before = snapshot(env)
        foreign = _consumer(checkout, descriptor_path, "foreign", evidence, journal)
        assert len(foreign["statuses"]) == 5 and foreign["ownerModelsHidden"] is True
        assert snapshot(env) == before
        journal.event("web-foreign-independent-readback", database=descriptor["scratchDatabase"], sql_objects_unchanged=True, statuses=foreign["statuses"])
        cleanup = _consumer(checkout, descriptor_path, "cleanup", evidence, journal)
        assert cleanup["providerAbsent"] and cleanup["modelsAbsent"] and cleanup["defaultsCleared"]
        with Session(env["engine"]) as db:
            assert db.scalar(select(func.count()).select_from(TenantModelProvider).where(TenantModelProvider.tenant_id == owner_id)) == 0
            assert db.scalar(select(func.count()).select_from(TenantLLM).where(TenantLLM.tenant_id == owner_id)) == 0
            assert db.get(TenantModel, identifier) is None
            tenant = db.get(Tenant, owner_id)
            assert tenant is not None and tenant.tenant_llm_id is None and not tenant.llm_id and tenant.tenant_ocr_id is None and not tenant.ocr_id
        journal.event("web-cleanup-independent-readback", database=descriptor["scratchDatabase"], provider_instance_model_absent=True, defaults_cleared=True)
        record_property("actual_web_provider_instance_sql", json.dumps({"web_sha": web_sha, "owner": result, "foreign": foreign, "cleanup": cleanup}))
    finally:
        for path in owned_files:
            if path.exists():
                path.unlink()
        private.rmdir()
        journal.event("consumer-private-cleanup", files_absent=all(not path.exists() for path in owned_files), directory_absent=not private.exists())


def test_actual_web_microsoft_connector_contract_and_foreign_denials(
    rest_documents: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, object], None]
) -> None:
    from api.db.db_models import Connector, SyncLogs
    from tests.support.teams_onedrive_fakes import CREDS, MicrosoftHTTP, install_graph

    env = rest_documents
    journal = env["journal"]
    checkout, web_sha = _pinned_checkout()
    teams, drive = MicrosoftHTTP("teams"), MicrosoftHTTP("onedrive")

    def graph(url: str, **kwargs: Any) -> Any:
        return (teams if "/v1.0/teams" in url else drive)(url, **kwargs)

    auth = install_graph(monkeypatch, graph)
    private = tmp_path / "microsoft-web-private"
    evidence = journal.path.parent / (journal.run_id + "-microsoft-web-consumer")
    journal.event("reserve-consumer-directories", private=str(private), evidence=str(evidence))
    private.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)
    descriptor_path = Path(os.environ.get("I_WEB_ACCEPTANCE_DESCRIPTOR_PATH", str(private / "descriptor.json")))
    files = [private / "owner.token", private / "foreign.token", private / "readback.json", descriptor_path]
    descriptor = {
        "origin": env["base"],
        "ownerTokenFile": str(files[0]),
        "foreignTokenFile": str(files[1]),
        "readbackFile": str(files[2]),
        "owners": {"ownerId": env["owners"][0], "foreignId": env["owners"][1]},
        "scratchDatabase": env["engine"].url.database,
        "connectorCredentials": CREDS,
        "connectorNames": ["web-teams-" + journal.run_id, "web-onedrive-" + journal.run_id],
    }
    owned_files: list[Path] = []
    script = "verify-microsoft-connector-routes.ts"
    try:
        _private_file(files[0], env["jwt"][0], journal, owned_files)
        _private_file(files[1], env["jwt"][1], journal, owned_files)
        _private_file(descriptor_path, json.dumps(descriptor), journal, owned_files)
        result = _consumer(checkout, descriptor_path, "owner", evidence, journal, script)
        _private_file(files[2], json.dumps(result), journal, owned_files)
        assert auth["constructed"] == auth["acquired"] == 2
        assert teams.calls and drive.calls
        identifiers = set(result["ids"].values())
        with Session(env["engine"]) as db:
            rows = list(db.scalars(select(Connector).where(Connector.tenant_id == env["owners"][0])))
            assert len(rows) == 2 and {row.id for row in rows} == identifiers
            for row in rows:
                config = row.config
                assert row.name in descriptor["connectorNames"]
                assert row.refresh_freq == 0 and row.prune_freq == 0
                assert config["sync_deleted_files"] is False and "batch_size" not in config and "sync_batch_size" not in config
                assert config["consumer_extension"] == {"retained": True, "version": 1}
                assert config["credentials"] == {**CREDS, "consumer_extension": {"retained": "opaque-microsoft-field"}}
                assert config["consumer_patch"] == row.source + "-updated"
                if row.source == "onedrive":
                    assert config["drive_ids"] == ["A", "B"] and config["folder_path"] == ""
            assert db.scalar(select(func.count()).select_from(SyncLogs)) == 0
        before = snapshot(env)
        counts = (len(teams.calls), len(drive.calls), dict(auth))
        foreign = _consumer(checkout, descriptor_path, "foreign", evidence, journal, script)
        assert foreign["ownerConnectorsHidden"] is True
        assert snapshot(env) == before
        assert (len(teams.calls), len(drive.calls), auth) == counts
        cleanup = _consumer(checkout, descriptor_path, "cleanup", evidence, journal, script)
        assert cleanup["connectorsAbsent"] is True
        with Session(env["engine"]) as db:
            assert db.scalar(select(func.count()).select_from(Connector).where(Connector.id.in_(identifiers))) == 0
            assert db.scalar(select(func.count()).select_from(SyncLogs)) == 0
        assert (len(teams.calls), len(drive.calls), auth) == counts
        journal.event(
            "microsoft-web-independent-readback",
            database=descriptor["scratchDatabase"],
            connectors=sorted(identifiers),
            auth_count=auth["constructed"],
            teams_requests=teams.calls,
            drive_requests=drive.calls,
            foreign_sql_objects_graph_unchanged=True,
            connectors_absent=True,
        )
        record_property("actual_web_microsoft_sql", json.dumps({"web_sha": web_sha, "owner": result, "foreign": foreign, "cleanup": cleanup}))
    finally:
        for path in owned_files:
            if path.exists():
                path.unlink()
        private.rmdir()
        journal.event("consumer-private-cleanup", files_absent=all(not path.exists() for path in owned_files), directory_absent=not private.exists())
