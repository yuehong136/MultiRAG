"""Saved connector probes use real authenticated HTTP and independent readback."""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from api.db.db_models import Connector
from common.constants import TaskStatus
from tests.support.rest_document_contract import call, snapshot
from tests.support.rest_document_contract import rest_documents as rest_documents
from tests.support.teams_onedrive_fakes import CREDS, MicrosoftHTTP, install_graph


@pytest.mark.parametrize("source", ["teams", "onedrive"])
def test_connection_probe_tenant_guard_and_zero_writes(rest_documents: dict[str, Any], monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    env = rest_documents
    identifier = uuid4().hex
    config = {"credentials": CREDS, **({"drive_ids": ["A", "B"]} if source == "onedrive" else {})}
    with Session(env["engine"]) as db:
        db.add(Connector(id=identifier, tenant_id=env["owners"][0], name="C test probe", source=source, input_type="poll", config=config, refresh_freq=5, status=TaskStatus.CANCEL))
        db.commit()
    http = MicrosoftHTTP(source)
    auth = install_graph(monkeypatch, http)
    before = snapshot(env)
    denied = call(env, "POST", f"/connectors/{identifier}/test", credential=env["jwt"][1])
    assert denied.status_code == 200 and denied.json()["retcode"] != 0
    assert auth["constructed"] == 0 and http.calls == []
    assert snapshot(env) == before
    accepted = call(env, "POST", f"/connectors/{identifier}/test", credential=env["jwt"][0])
    assert accepted.status_code == 200 and accepted.json()["retcode"] == 0, accepted.text
    assert http.calls and auth["constructed"] == 1
    assert snapshot(env) == before
    http.mode = "permission"
    failed = call(env, "POST", f"/connectors/{identifier}/test", credential=env["jwt"][0])
    assert failed.status_code == 200 and failed.json()["retcode"] != 0
    assert "controlled-secret-must-not-leak" not in failed.text
    assert snapshot(env) == before
    env["journal"].event(
        "probe-readback", source=source, denied=denied.json()["retcode"], accepted=accepted.json()["retcode"], failed=failed.json()["retcode"], sql_object_unchanged=True, requests=http.calls
    )
