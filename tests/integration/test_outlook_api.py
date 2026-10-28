"""Public Outlook config/probe auth and failure zero-write readback."""

from typing import Any

import pytest

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
