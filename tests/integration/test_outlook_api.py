"""Public Outlook config/probe auth and failure zero-write readback."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.db.db_models import Connector
from tests.support.outlook_fakes import OutlookHTTP
from tests.support.rest_document_contract import call, snapshot
from tests.support.rest_document_contract import rest_documents as rest_documents
from tests.support.teams_onedrive_fakes import CREDS, install_graph


def test_probe_tenant_guard_empty_and_permission_zero_writes(rest_documents: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = rest_documents
    http = OutlookHTTP()
    auth = install_graph(monkeypatch, http)
    config = {"credentials": CREDS, "user_ids": [], "folder": "inbox", "sync_deleted_files": False}
    created = call(env, "POST", "/connectors", json={"source": "outlook", "name": "Outlook probe", "config": config})
    assert created.json()["retcode"] == 0, created.text
    identifier = created.json()["data"]["id"]
    assert not http.calls and auth["constructed"] == 0
    before = snapshot(env)
    for credential in (None, env["jwt"][1]):
        denied = call(env, "POST", f"/connectors/{identifier}/test", credential=credential)
        assert denied.status_code in {200, 401, 403}
        assert denied.status_code != 200 or denied.json()["retcode"] != 0
        assert not http.calls and auth["constructed"] == 0 and snapshot(env) == before
    empty = call(env, "POST", f"/connectors/{identifier}/test", credential=env["jwt"][0])
    assert empty.json()["retcode"] != 0 and not http.calls and snapshot(env) == before
    config["user_ids"] = ["A", "B"]
    saved = call(env, "PATCH", f"/connectors/{identifier}", json={"config": config})
    assert saved.json()["retcode"] == 0, saved.text
    before = snapshot(env)
    accepted = call(env, "POST", f"/connectors/{identifier}/test", credential=env["jwt"][0])
    assert accepted.json()["retcode"] == 0, accepted.text
    assert len(http.calls) == 2 and auth["constructed"] == 1 and snapshot(env) == before
    for mode in ("permission", "unauthorized", "not-found", "server-error", "json", "malformed"):
        http.mode = mode
        failed = call(env, "POST", f"/connectors/{identifier}/test", credential=env["jwt"][0])
        assert failed.json()["retcode"] != 0
        assert "controlled-secret-must-not-leak" not in failed.text and snapshot(env) == before
    env["journal"].event("outlook-probe-readback", denied_without_auth=True, empty_without_auth=True, accepted=accepted.json()["retcode"], sql_object_queue_unchanged=True, requests=http.calls)


@pytest.mark.parametrize(
    "update",
    [
        {"user_ids": None},
        {"user_ids": "A,B"},
        {"user_ids": ["A", "A"]},
        {"folder": None},
        {"batch_size": True},
        {"batch_size": "1"},
        {"batch_size": 0},
        {"sync_batch_size": None},
        {"credentials": None},
        {"sync_deleted_files": True},
    ],
)
def test_invalid_create_update_has_zero_writes(rest_documents: dict[str, Any], monkeypatch: pytest.MonkeyPatch, update: dict[str, Any]) -> None:
    env = rest_documents
    http = OutlookHTTP()
    auth = install_graph(monkeypatch, http)
    config = {"user_ids": ["A"], "credentials": CREDS}
    before = snapshot(env)
    invalid = call(env, "POST", "/connectors", json={"source": "outlook", "name": "invalid", "config": {**config, **update}})
    assert invalid.json()["retcode"] != 0 and snapshot(env) == before
    created = call(env, "POST", "/connectors", json={"source": "outlook", "name": "valid", "config": config})
    assert created.json()["retcode"] == 0, created.text
    identifier = created.json()["data"]["id"]
    before = snapshot(env)
    invalid = call(env, "PATCH", f"/connectors/{identifier}", json={"config": {**config, **update}})
    assert invalid.json()["retcode"] != 0 and snapshot(env) == before
    assert not http.calls and auth["constructed"] == 0


@pytest.mark.external_consumer
def test_fixed_web_domain_client_with_independent_readback(rest_documents: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = rest_documents
    checkout = Path(os.environ.get("WEB_COMPLETION_CHECKOUT", "/Users/xldu/project/web"))
    expected = os.environ.get("WEB_OUTLOOK_SHA") or json.loads((Path(__file__).resolve().parents[2] / ".github/consumer-web.json").read_text())["ref"]
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    assert actual == expected, "Web checkout must match the frozen acceptance SHA"
    assert subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=checkout, text=True) == ""
    runner = shutil.which("node")
    loader = checkout / "node_modules/tsx/dist/loader.mjs"
    script = checkout / "scripts/verify-outlook-connector-routes.ts"
    assert runner is not None and loader.exists() and script.exists()
    http = OutlookHTTP()
    install_graph(monkeypatch, http)
    directory = Path(os.environ.get("TMPDIR", tempfile.gettempdir())) / ("outlook-web-" + env["journal"].run_id)
    paths = {key: directory / name for key, name in {"ownerTokenFile": "owner.jwt", "foreignTokenFile": "foreign.jwt", "readbackFile": "readback.json", "descriptor": "descriptor.json"}.items()}
    env["journal"].event("reserve-outlook-web-descriptor", directory=str(directory), files={key: str(path) for key, path in paths.items()}, web_sha=actual, script=str(script))
    directory.mkdir(mode=0o700)
    try:
        for key, token in zip(("ownerTokenFile", "foreignTokenFile"), env["jwt"], strict=True):
            paths[key].write_text(token)
            paths[key].chmod(0o600)
        descriptor = {
            "origin": env["base"],
            "ownerTokenFile": str(paths["ownerTokenFile"]),
            "foreignTokenFile": str(paths["foreignTokenFile"]),
            "owners": {"ownerId": env["owners"][0], "foreignId": env["owners"][1]},
            "scratchDatabase": env["engine"].url.database,
            "readbackFile": str(paths["readbackFile"]),
            "connectorCredentials": CREDS,
            "forbiddenMailbox": "forbidden",
        }
        paths["descriptor"].write_text(json.dumps(descriptor))
        paths["descriptor"].chmod(0o600)
        before = snapshot(env)

        def run(mode: str) -> dict[str, Any]:
            env["journal"].event("reserve-web-client-process", mode=mode, executable=str(runner), script=str(script), cwd=str(checkout), session="inherits registered runner process group")
            completed = subprocess.run(
                [runner, "--import", str(loader), str(script)],
                cwd=checkout,
                env={
                    **os.environ,
                    "I_WEB_ACCEPTANCE_MODE": mode,
                    "I_WEB_ACCEPTANCE_DESCRIPTOR": str(paths["descriptor"]),
                    "TSX_TSCONFIG_PATH": str(checkout / "tsconfig.app.json"),
                    "MULTIRAG_RUN_JOURNAL": os.environ.get("MULTIRAG_RUN_JOURNAL", str(env["journal"].path)),
                },
                capture_output=True,
                text=True,
                timeout=90,
            )
            env["journal"].event("web-client-raw-result", mode=mode, raw_exit=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
            assert completed.returncode == 0, completed.stdout + completed.stderr
            result = json.loads(completed.stdout.splitlines()[-1])
            assert result["status"] == "passed"
            return result

        owner = run("owner")
        paths["readbackFile"].write_text(json.dumps(owner))
        paths["readbackFile"].chmod(0o600)
        identifier = owner["ids"]["outlook"]
        with Session(env["engine"]) as db:
            connector = db.get(Connector, identifier)
            assert connector.tenant_id == env["owners"][0] and connector.source == "outlook"
            assert connector.config["user_ids"] == ["A", "B"] and connector.config["folder"] == "inbox"
            assert connector.config["sync_deleted_files"] is False
            assert connector.config["consumer_extension"] == {"zero": 0} and connector.config["credentials"]["consumer_extension"] == "retained"
        saved = snapshot(env)
        assert saved["objects"] == before["objects"]
        assert all(rows == before["sql"][table] for table, rows in saved["sql"].items() if table != Connector.__tablename__)
        assert len(http.calls) == 3 and "/users/forbidden/" in http.calls[-1]
        requests = list(http.calls)
        foreign = run("foreign")
        assert snapshot(env) == saved and http.calls == requests
        cleanup = run("cleanup")
        assert snapshot(env) == before and http.calls == requests
        env["journal"].event("fixed-web-outlook-readback", web_sha=actual, owner=owner, foreign=foreign, cleanup=cleanup, sql_object_unchanged_for_denials=True, requests=http.calls)
    finally:
        shutil.rmtree(directory)
        env["journal"].event("outlook-web-descriptor-cleanup", directory=str(directory), absent=not directory.exists())
        assert not directory.exists()
