"""Registered connector routes with real identity, SQL and object readback."""

import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, File, File2Document, Knowledgebase, SyncLogs, Task, UserTenant
from api.db.services.connector_service import ConnectorAuthorizationError, ConnectorService, SyncLogsService
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from common.constants import FileSource, RetCode, TaskStatus
from common.data_source.rest_api_connector import RestAPIConnector
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api
from tests.support.web_consumer import tsx_runner

pytestmark = pytest.mark.integration


@pytest.fixture
def connector_authorization_api(connector_sync_api: dict[str, Any]) -> Iterator[dict[str, Any]]:
    from api.apps import manager

    env = connector_sync_api
    owner, guest = env["owners"]
    connector_id, neighbor_id, foreign_kb, unbound_kb, membership_id = (uuid4().hex for _ in range(5))
    kb_ids = [env["kb"], foreign_kb, unbound_kb]
    connector_ids = [connector_id, neighbor_id]
    doc_ids: dict[str, str] = {}
    log_ids: dict[str, str] = {}
    record: dict[str, Any] = {
        "connector_ids": connector_ids,
        "kb_ids": kb_ids,
        "membership_id": membership_id,
        "documents": doc_ids,
        "logs": log_ids,
    }
    env["record"]["connector_authorization"] = record

    def remove_rows() -> None:
        with Session(env["engine"]) as db:
            scoped_docs = sa.select(Document.id).where(Document.kb_id.in_(kb_ids))
            db.execute(sa.delete(Task).where(Task.doc_id.in_(scoped_docs)))
            db.execute(sa.delete(File2Document).where(File2Document.document_id.in_(scoped_docs)))
            db.execute(sa.delete(Document).where(Document.kb_id.in_(kb_ids)))
            db.execute(sa.delete(File).where(File.tenant_id.in_(env["owners"])))
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id.in_(connector_ids)))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.connector_id.in_(connector_ids)))
            db.execute(sa.delete(Connector).where(Connector.id.in_(connector_ids)))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id.in_([foreign_kb, unbound_kb])))
            db.execute(sa.delete(UserTenant).where(UserTenant.id == membership_id))
            db.commit()
        with env["engine"].connect() as db:
            remaining = {
                "connectors": db.scalar(sa.select(sa.func.count()).select_from(Connector).where(Connector.id.in_(connector_ids))),
                "logs": db.scalar(sa.select(sa.func.count()).select_from(SyncLogs).where(SyncLogs.connector_id.in_(connector_ids))),
                "documents": db.scalar(sa.select(sa.func.count()).select_from(Document).where(Document.kb_id.in_(kb_ids))),
                "files": db.scalar(sa.select(sa.func.count()).select_from(File).where(File.tenant_id.in_(env["owners"]))),
                "membership": db.scalar(sa.select(sa.func.count()).select_from(UserTenant).where(UserTenant.id == membership_id)),
                "extra_datasets": db.scalar(sa.select(sa.func.count()).select_from(Knowledgebase).where(Knowledgebase.id.in_([foreign_kb, unbound_kb]))),
            }
            assert not any(remaining.values()), remaining
        record["sql_cleanup"] = remaining

    with ExitStack() as cleanup:
        cleanup.callback(remove_rows)
        with Session(env["engine"]) as db:
            for kb_id, tenant in [(foreign_kb, guest), (unbound_kb, owner)]:
                db.add(Knowledgebase(id=kb_id, tenant_id=tenant, created_by=tenant, name=f"connector_auth_{kb_id}", embd_id="controlled", parser_id="naive", permission="team"))
            db.get(Knowledgebase, env["kb"]).permission = "team"
            for identifier in connector_ids:
                db.add(
                    Connector(
                        id=identifier,
                        tenant_id=owner,
                        name="authorization scratch",
                        source=FileSource.REST_API,
                        input_type="poll",
                        config={"credentials": {"password": "owned-credential-must-not-leak"}},
                    )
                )
            # A stale cross-tenant binding cannot grant access to that dataset.
            for kb_id in [env["kb"], foreign_kb]:
                db.add(Connector2Kb(id=uuid4().hex, connector_id=connector_id, kb_id=kb_id))
            db.commit()
            for name, kb_id, tenant, connector in [
                ("target", env["kb"], owner, connector_id),
                ("neighbor", env["kb"], owner, neighbor_id),
                ("foreign", foreign_kb, guest, connector_id),
                ("unbound", unbound_kb, owner, connector_id),
            ]:
                identifier = doc_ids[name] = uuid4().hex
                errors, created = SyncLogsService.duplicate_and_parse(
                    db,
                    db.get(Knowledgebase, kb_id),
                    [{"id": identifier, "semantic_identifier": name, "extension": ".txt", "blob": name.encode()}],
                    tenant,
                    f"{FileSource.REST_API}/{connector}",
                    auto_parse=False,
                )
                assert not errors and created == [identifier]
                log_id = log_ids[name] = uuid4().hex
                db.add(SyncLogs(id=log_id, connector_id=connector, kb_id=kb_id, status=TaskStatus.DONE))
            db.commit()
            locations = {doc.id: (doc.kb_id, doc.location) for doc in db.scalars(sa.select(Document).where(Document.id.in_(doc_ids.values())))}
        env.update(
            auth_connector=connector_id,
            auth_neighbor=neighbor_id,
            auth_foreign_kb=foreign_kb,
            auth_unbound_kb=unbound_kb,
            auth_member=membership_id,
            auth_docs=doc_ids,
            auth_logs=log_ids,
            auth_locations=locations,
            auth_guest_jwt=manager.create_access_token(data={"sub": f"{guest}@upload.test"}),
        )
        yield env


def _set_membership(env: dict[str, Any], role: str | None, status: str = "1") -> None:
    with Session(env["engine"]) as db:
        db.execute(sa.delete(UserTenant).where(UserTenant.id == env["auth_member"]))
        if role is not None:
            db.add(UserTenant(id=env["auth_member"], user_id=env["owners"][1], tenant_id=env["owners"][0], role=role, status=status, invited_by=env["owners"][0]))
        db.commit()


def _snapshot(env: dict[str, Any]) -> dict[str, Any]:
    connector_ids = [env["auth_connector"], env["auth_neighbor"]]
    kb_ids = [env["kb"], env["auth_foreign_kb"], env["auth_unbound_kb"]]
    scoped = [
        (Connector, Connector.id.in_(connector_ids)),
        (Connector2Kb, Connector2Kb.connector_id.in_(connector_ids)),
        (SyncLogs, SyncLogs.connector_id.in_(connector_ids)),
        (Document, Document.kb_id.in_(kb_ids)),
        (File2Document, File2Document.document_id.in_(env["auth_docs"].values())),
        (File, File.tenant_id.in_(env["owners"])),
        (Task, Task.doc_id.in_(env["auth_docs"].values())),
        (Knowledgebase, Knowledgebase.id.in_(kb_ids)),
    ]
    with env["engine"].connect() as reader:
        sql = {model.__tablename__: sorted(repr(tuple(row)) for row in reader.execute(sa.select(model).where(condition))) for model, condition in scoped}
    objects = {item.object_name: sha256(read_object(env["storage"], env["bucket"], item.object_name)).hexdigest() for item in env["storage"].list_objects(env["bucket"], recursive=True)}
    return {"sql": sql, "objects": objects}


def _fence_side_effects(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    touched: list[str] = []

    def forbidden(name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> NoReturn:
            touched.append(name)
            raise AssertionError(f"Denied request reached {name}")

        return fail

    for service, method in [
        (SyncLogsService, "filter_delete"),
        (SyncLogsService, "schedule"),
        (FileService, "delete_docs"),
        (DocumentService, "query"),
        (ConnectorService, "update_by_id"),
        (ConnectorService, "cancel_tasks"),
        (ConnectorService, "schedule_tasks"),
        (ConnectorService, "delete_by_id"),
    ]:
        monkeypatch.setattr(service, method, classmethod(forbidden(method)))
    for method in ["__init__", "parse_storage_config", "from_parsed_config", "validate_config"]:
        monkeypatch.setattr(RestAPIConnector, method, forbidden(method))
    return touched


def _assert_denied(response: requests.Response) -> None:
    assert response.status_code == 200, response.text
    assert response.json() == {"retcode": RetCode.AUTHENTICATION_ERROR, "retmsg": "No authorization.", "data": False}, response.text
    assert "owned-credential-must-not-leak" not in response.text


def test_all_connector_routes_use_live_membership_before_side_effects(connector_authorization_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    env = connector_authorization_api
    rest = f"/api/v1/connectors/{env['auth_connector']}"
    legacy = f"/v1/connector/{env['auth_connector']}"
    routes = [
        ("GET", rest, None),
        ("PATCH", rest, {"config": {"credentials": {"password": "denied-update"}}}),
        ("DELETE", rest, None),
        ("GET", f"{rest}/logs", None),
        ("PATCH", rest, {"status": "5"}),
        ("PATCH", rest, {"status": "2"}),
        ("PATCH", rest, {"reschedule": True}),
        ("POST", f"{rest}/rebuild", {"kb_id": env["kb"]}),
        ("POST", f"{rest}/test", None),
        ("GET", legacy, None),
        ("GET", f"{legacy}/logs", None),
        ("PUT", f"{legacy}/rebuild", {"kb_id": env["kb"]}),
        ("POST", f"{legacy}/rm", None),
        ("POST", "/v1/connector/set", {"id": env["auth_connector"], "config": {"credentials": {"password": "denied-update"}}}),
    ]
    touched = _fence_side_effects(monkeypatch)
    cases = [("foreign", None, "1"), ("inactive", "normal", "0"), ("pending", "invite", "1"), ("unknown-role", "unknown", "1")]
    evidence: list[dict[str, Any]] = []
    for name, role, status in cases:
        _set_membership(env, role, status)
        before = _snapshot(env)
        headers = {"Authorization": f"Bearer {env['auth_guest_jwt']}"}
        for method, path, body in routes:
            _assert_denied(requests.request(method, env["base"] + path, json=body, headers=headers, timeout=30))
        assert _snapshot(env) == before
        assert touched == []
        evidence.append({"case": name, "denied_routes": len(routes), "sql_objects_unchanged": True, "side_effect_calls": list(touched)})

    before = _snapshot(env)
    for method, path, body in routes:
        response = requests.request(method, env["base"] + path, json=body, timeout=30)
        assert response.status_code == 401
        assert "owned-credential-must-not-leak" not in response.text
    assert _snapshot(env) == before and touched == []
    assert "owned-credential-must-not-leak" not in caplog.text
    env["record"]["connector_authorization"]["route_denials"] = evidence + [{"case": "anonymous", "denied_routes": len(routes), "sql_objects_unchanged": True}]


def test_rebuild_checks_kb_visibility_and_binding_before_deletion(connector_authorization_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    env = connector_authorization_api
    touched = _fence_side_effects(monkeypatch)
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    cases = [("foreign-bound", env["auth_foreign_kb"]), ("owned-unbound", env["auth_unbound_kb"]), ("missing", uuid4().hex)]
    with Session(env["engine"]) as db:
        db.get(Knowledgebase, env["kb"]).status = "0"
        db.commit()
    cases.append(("inactive", env["kb"]))
    for name, kb_id in cases:
        before = _snapshot(env)
        for method, path in [("POST", f"/api/v1/connectors/{env['auth_connector']}/rebuild"), ("PUT", f"/v1/connector/{env['auth_connector']}/rebuild")]:
            _assert_denied(requests.request(method, env["base"] + path, json={"kb_id": kb_id}, headers=headers, timeout=30))
        assert _snapshot(env) == before and touched == []
    with Session(env["engine"]) as db:
        kb = db.get(Knowledgebase, env["kb"])
        kb.status, kb.permission = "1", "me"
        db.commit()
    _set_membership(env, "normal")
    before = _snapshot(env)
    _assert_denied(
        requests.post(f"{env['base']}/api/v1/connectors/{env['auth_connector']}/rebuild", json={"kb_id": env["kb"]}, headers={"Authorization": f"Bearer {env['auth_guest_jwt']}"}, timeout=30)
    )
    assert _snapshot(env) == before and touched == []
    _assert_denied(requests.post(f"{env['base']}/api/v1/connectors/{uuid4().hex}/rebuild", json={"kb_id": env["kb"]}, headers=headers, timeout=30))
    assert _snapshot(env) == before and touched == []
    assert "owned-credential-must-not-leak" not in caplog.text
    env["record"]["connector_authorization"]["rebuild_denials"] = {
        "cases": [name for name, _ in cases] + ["private-team-member", "missing-connector"],
        "sql_objects_unchanged": True,
        "side_effect_calls": touched,
    }


@pytest.mark.parametrize("role", ["owner", "normal", "admin"])
@pytest.mark.parametrize("gateway", ["rest", "legacy"])
def test_authorized_bound_rebuild_commits_scoped_cleanup_and_schedule(connector_authorization_api: dict[str, Any], role: str, gateway: str) -> None:
    env = connector_authorization_api
    if role != "owner":
        _set_membership(env, role)
    if role in {"owner", "admin"}:
        with Session(env["engine"]) as db:
            db.get(Knowledgebase, env["kb"]).permission = "me"
            db.commit()
    token = env["jwt"] if role == "owner" else env["auth_guest_jwt"]
    method, path = ("POST", f"/api/v1/connectors/{env['auth_connector']}/rebuild") if gateway == "rest" else ("PUT", f"/v1/connector/{env['auth_connector']}/rebuild")
    response = requests.request(method, env["base"] + path, json={"kb_id": env["kb"]}, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    assert response.status_code == 200
    assert response.json() == {"retcode": RetCode.SUCCESS, "retmsg": "success", "data": True}, response.text
    _assert_rebuild_readback(env, f"{gateway} {role}")


def _assert_rebuild_readback(env: dict[str, Any], role: str) -> None:
    with Session(env["engine"]) as reader:
        assert reader.get(Document, env["auth_docs"]["target"]) is None
        assert reader.get(SyncLogs, env["auth_logs"]["target"]) is None
        logs = list(reader.scalars(sa.select(SyncLogs).where(SyncLogs.connector_id == env["auth_connector"], SyncLogs.kb_id == env["kb"])))
        assert len(logs) == 1 and logs[0].status == TaskStatus.SCHEDULE and logs[0].from_beginning == "1"
        assert reader.get(Connector, env["auth_connector"]).status == TaskStatus.SCHEDULE
        assert reader.get(Knowledgebase, env["kb"]).doc_num == 1
        for name in ["neighbor", "foreign", "unbound"]:
            assert reader.get(Document, env["auth_docs"][name]) is not None
            assert reader.get(SyncLogs, env["auth_logs"][name]) is not None
        scheduled_id = logs[0].id
    objects = {obj.object_name for obj in env["storage"].list_objects(env["bucket"], recursive=True)}
    assert objects == {f"{kb}/{location}" for identifier, (kb, location) in env["auth_locations"].items() if identifier != env["auth_docs"]["target"]}
    for name in ["neighbor", "foreign", "unbound"]:
        kb, location = env["auth_locations"][env["auth_docs"][name]]
        assert read_object(env["storage"], env["bucket"], f"{kb}/{location}") == name.encode()
    env["record"]["connector_authorization"]["success"] = {"role": role, "scheduled_id": scheduled_id, "owned_document_object_removed": True, "other_scopes_retained": True}


def test_direct_rebuild_cannot_bypass_resource_authorization(connector_authorization_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = connector_authorization_api
    touched = _fence_side_effects(monkeypatch)
    before = _snapshot(env)
    for connector_id, kb_id, user_id in [
        (env["auth_connector"], env["kb"], env["owners"][1]),
        (env["auth_connector"], env["auth_foreign_kb"], env["owners"][0]),
        (env["auth_connector"], env["auth_unbound_kb"], env["owners"][0]),
        (uuid4().hex, env["kb"], env["owners"][0]),
        (env["auth_connector"], uuid4().hex, env["owners"][0]),
    ]:
        with Session(env["engine"]) as db, pytest.raises(ConnectorAuthorizationError, match=r"^No authorization\.$"):
            ConnectorService.rebuild(db, connector_id, kb_id, user_id)
    assert _snapshot(env) == before and touched == []


@pytest.mark.external_consumer
def test_real_web_rebuild_consumer_handles_denial_and_success(connector_authorization_api: dict[str, Any], tmp_path: Path) -> None:
    env = connector_authorization_api
    checkout = Path(os.environ.get("WEB_DATASET_CHECKOUT", str(Path(__file__).resolve().parents[3] / "web"))).resolve()
    runner = tsx_runner(checkout)
    assert runner.is_file(), "Installed Web consumer is required"
    script = tmp_path / "connector-rebuild-consumer.mts"
    script.write_text(
        """import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
const input = JSON.parse(process.env.CONNECTOR_AUTH_INPUT!)
const { datasourceAPI } = await import(pathToFileURL(input.checkout + '/src/api/datasource.ts').href)
const { apiClient, APIError } = await import(pathToFileURL(input.checkout + '/src/api/client.ts').href)
apiClient.setAuthToken(input.token)
const originalFetch = globalThis.fetch
const calls: { path: string, method: string, body: unknown }[] = []
globalThis.fetch = async (url, init) => {
  const parsed = new URL(String(url))
  const expected = `/api/v1/connectors/${input.connector}/rebuild`
  assert.equal(parsed.pathname, expected, 'No fallback or unrelated endpoint is allowed')
  assert.equal(init?.method, 'POST')
  calls.push({ path: parsed.pathname, method: init!.method!, body: JSON.parse(String(init?.body)) })
  return originalFetch(input.base + parsed.pathname + parsed.search, init)
}
try {
  for (const kb of [input.foreign, input.unbound]) {
    await assert.rejects(
      () => datasourceAPI.connector.rebuild(input.connector, kb),
      (error: unknown) => error instanceof APIError && error.code === '109',
    )
  }
  assert.equal(calls.length, 2, 'A business denial must not trigger legacy fallback')
  await datasourceAPI.connector.rebuild(input.connector, input.kb)
  assert.deepEqual(calls.map(call => call.body), [
    { kb_id: input.foreign }, { kb_id: input.unbound }, { kb_id: input.kb },
  ])
  console.log(JSON.stringify({ calls, deniedRetcode: '109', success: true }))
} finally {
  globalThis.fetch = originalFetch
  apiClient.setAuthToken(null)
}
"""
    )
    payload = {
        "checkout": str(checkout),
        "token": env["jwt"],
        "base": env["base"],
        "connector": env["auth_connector"],
        "foreign": env["auth_foreign_kb"],
        "unbound": env["auth_unbound_kb"],
        "kb": env["kb"],
    }
    result = subprocess.run(
        [str(runner), "--tsconfig", str(checkout / "tsconfig.app.json"), str(script)],
        cwd=checkout,
        env={**os.environ, "CONNECTOR_AUTH_INPUT": json.dumps(payload)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result_data = json.loads(result.stdout.strip().splitlines()[-1])
    assert result_data["success"] and len(result_data["calls"]) == 3
    _assert_rebuild_readback(env, "Web owner")
    env["record"]["connector_authorization"]["web_consumer"] = result_data
