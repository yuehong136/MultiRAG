"""Registered Agent file HTTP with real identities, scratch SQL and owned bytes."""

import asyncio
import copy
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import API4Conversation, APIToken, Tenant, User, UserCanvas, UserTenant
from api.db.services.file_service import FileService
from common import resources, settings
from core.utils.encrypted_storage import EncryptedStorageWrapper
from tests.support.agent_update_release import message_dsl
from tests.support.agent_update_release import release_api as release_api


@pytest.fixture
def agent_files(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    env = release_api
    owners = env["owners"]
    agents = {name: uuid4().hex for name in ["private", "team", "foreign"]}
    beta = {name: "agent-files-" + uuid4().hex for name in ["owner", "foreign", "duplicate", "orphan", "collision"]}
    token_ids = []
    record = {"database": env["engine"].url.database, "owners": owners, "agents": agents, "base": env["base"], "bucket": "agent-files-" + uuid4().hex, "pid": os.getpid()}
    evidence = Path(os.environ.get("MULTIRAG_D_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    record_path = evidence / f"{agents['private']}.resources.json"

    def save() -> None:
        record_path.write_text(json.dumps(record, indent=2))

    source = settings.STORAGE_IMPL
    storage = copy.copy(source)
    adapter = copy.copy(source.storage_impl) if isinstance(source, EncryptedStorageWrapper) else storage
    if isinstance(source, EncryptedStorageWrapper):
        storage.storage_impl = adapter
    adapter.bucket, adapter.prefix_path = record["bucket"], ""
    client = adapter.conn
    monkeypatch.setitem(resources._state, "storage", storage)
    io: list[str] = []
    writes: list[str] = []

    def sql_write(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if statement.lstrip().split(maxsplit=1)[0].lower() in {"insert", "update", "delete"}:
            writes.append(statement.split(maxsplit=1)[0].lower())

    for method in ["get", "put", "rm", "obj_exist"]:
        original = getattr(storage, method)

        def observed(*args: Any, _original: Any = original, _method: str = method, **kwargs: Any) -> Any:
            io.append(_method)
            return _original(*args, **kwargs)

        monkeypatch.setattr(storage, method, observed)
    with ExitStack() as cleanup:
        cleanup.callback(save)

        def remove_rows() -> None:
            with Session(env["engine"]) as db:
                db.execute(sa.delete(APIToken).where(APIToken.token.in_(token_ids)))
                db.commit()
            with env["engine"].connect() as db:
                assert db.scalar(sa.select(sa.func.count()).select_from(APIToken).where(APIToken.token.in_(token_ids))) == 0
            record["tokens_removed"] = True

        def remove_bucket() -> None:
            if client.bucket_exists(record["bucket"]):
                for item in client.list_objects(record["bucket"], recursive=True):
                    client.remove_object(record["bucket"], item.object_name)
                client.remove_bucket(record["bucket"])
            assert not client.bucket_exists(record["bucket"])
            record["bucket_removed"] = True

        cleanup.callback(remove_rows)
        cleanup.callback(remove_bucket)
        save()
        client.make_bucket(record["bucket"])
        with Session(env["engine"]) as db:
            for name, agent_id in agents.items():
                db.add(UserCanvas(id=agent_id, user_id=owners[1] if name == "foreign" else owners[0], title=name, permission="team" if name == "team" else "me", dsl=message_dsl("file run")))
            db.add(UserTenant(id=uuid4().hex, user_id=owners[1], tenant_id=owners[0], role="normal", status="1", invited_by=owners[0]))
            for name, owner in [("owner", owners[0]), ("foreign", owners[1]), ("duplicate", owners[0]), ("duplicate", owners[1]), ("orphan", uuid4().hex), ("collision", owners[0])]:
                token = beta[name] if name == "collision" else "agent-key-" + uuid4().hex
                token_ids.append(token)
                db.add(APIToken(tenant_id=owner, token=token, beta=beta[name], name="agent-file-test"))
            db.commit()
        sa.event.listen(sa.engine.Engine, "before_cursor_execute", sql_write)
        cleanup.callback(sa.event.remove, sa.engine.Engine, "before_cursor_execute", sql_write)
        yield dict(env, agents=agents, beta=beta, bucket=record["bucket"], storage=storage, object_client=client, io=io, writes=writes, record=record, record_path=record_path)


def snapshot(env: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    sql = []
    with env["engine"].connect() as db:
        for model in [UserCanvas, API4Conversation, APIToken, UserTenant, Tenant, User]:
            rows = [dict(row._mapping) for row in db.execute(sa.select(model.__table__))]
            sql.append(hashlib.sha256(json.dumps(sorted(rows, key=str), default=str, sort_keys=True).encode()).hexdigest())
    objects = {}
    for item in env["object_client"].list_objects(env["bucket"], recursive=True):
        response = env["object_client"].get_object(env["bucket"], item.object_name)
        try:
            objects[item.object_name] = hashlib.sha256(response.read()).hexdigest()
        finally:
            response.close()
            response.release_conn()
    return sql, objects


def upload(env: dict[str, Any], *, agent: str = "private", credential: str | None = None, **kwargs: Any) -> requests.Response:
    return requests.post(
        f"{env['base']}/api/v1/agents/{env['agents'].get(agent, agent)}/upload",
        headers={"Authorization": credential} if credential is not None else {},
        timeout=30,
        **(kwargs or {"files": {"file": ("one.txt", b"one", "text/plain")}}),
    )


def test_auth_matrix_denies_before_file_network_write_or_runtime(agent_files: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = agent_files
    crawls: list[str] = []

    def denied_crawl(url: str) -> Any:
        crawls.append(url)
        raise AssertionError("Denied request reached outbound crawl")

    monkeypatch.setattr(FileService, "_resolve_safe_crawl_url", denied_crawl)
    before = snapshot(env)
    task_count = len(env["task_ids"])
    env["io"].clear()
    env["writes"].clear()
    for credential in [
        None,
        "Bearer",
        "Basic invalid",
        "Bearer invalid extra",
        "Bearer invalid",
        f"Bearer {env['beta']['duplicate']}",
        f"Bearer {env['beta']['orphan']}",
        f"Bearer {env['beta']['collision']}",
        f"Bearer {env['beta']['foreign']}",
    ]:
        response = upload(env, credential=credential, params={"url": "http://169.254.169.254/latest", "created_by": env["owners"][0], "user_id": env["owners"][0]})
        assert response.status_code in {200, 401}
        body = response.json()
        assert body.get("retcode", body.get("code")) != 0
    for agent, credential in [("private", env["jwts"][1]), ("foreign", env["jwts"][0]), ("missing", env["jwts"][0]), ("foreign", env["beta"]["owner"])]:
        response = upload(env, agent=agent, credential=f"Bearer {credential}")
        assert response.json().get("retcode", response.json().get("code")) != 0
    # Multiple Authorization fields must not select whichever credential happens
    # to be returned first by Headers.get().
    import httpx

    response = httpx.post(
        f"{env['base']}/api/v1/agents/{env['agents']['private']}/upload",
        headers=[("Authorization", f"Bearer {env['jwts'][0]}"), ("Authorization", f"Bearer {env['beta']['owner']}")],
        files={"file": ("bad.txt", b"bad")},
    )
    assert response.json().get("code") != 0
    assert not env["io"] and not env["writes"] and not crawls
    assert len(env["task_ids"]) == task_count and snapshot(env) == before


def test_active_owner_team_api_and_beta_write_authenticated_namespaces(agent_files: dict[str, Any]) -> None:
    env = agent_files
    for agent, credential, owner in [
        ("private", env["jwts"][0], env["owners"][0]),
        ("team", env["jwts"][1], env["owners"][1]),
        ("team", env["keys"][1], env["owners"][1]),
        ("private", env["beta"]["owner"], env["owners"][0]),
    ]:
        response = upload(
            env,
            agent=agent,
            credential=f"Bearer {credential}",
            params={"created_by": "forged", "user_id": "forged"},
            files=[("file", ("first.txt", b"first", "text/plain")), ("file", ("second.txt", b"second", "text/plain"))],
        )
        assert response.status_code == 200 and response.json()["retcode"] == 0
        descriptors = response.json()["data"]
        assert [d["name"] for d in descriptors] == ["first.txt", "second.txt"]
        assert all(d["created_by"] == owner for d in descriptors)
        assert asyncio.run(FileService.resolve_runtime_uploads(owner, [d["id"] for d in descriptors])) == descriptors
        for descriptor, content in zip(descriptors, [b"first", b"second"], strict=True):
            response = env["object_client"].get_object(env["bucket"], f"{owner}-downloads/{descriptor['id']}")
            try:
                assert response.read() == content
            finally:
                response.close()
                response.release_conn()


@pytest.mark.parametrize(("role", "status", "allowed"), [("admin", "1", True), ("normal", "0", False), ("admin", "0", False), ("invite", "1", False)])
def test_team_membership_boundary(agent_files: dict[str, Any], role: str, status: str, allowed: bool) -> None:
    env = agent_files
    with Session(env["engine"]) as db:
        db.execute(sa.update(UserTenant).where(UserTenant.tenant_id == env["owners"][0], UserTenant.user_id == env["owners"][1]).values(role=role, status=status))
        db.commit()
    before = snapshot(env)
    env["io"].clear()
    env["writes"].clear()
    response = upload(env, agent="team", credential=f"Bearer {env['jwts'][1]}")
    assert (response.json()["retcode"] == 0) is allowed
    if not allowed:
        assert not env["io"] and not env["writes"] and snapshot(env) == before


def test_inactive_beta_tenant_denied_without_object_io(agent_files: dict[str, Any]) -> None:
    env = agent_files
    with Session(env["engine"]) as db:
        db.execute(sa.update(Tenant).where(Tenant.id == env["owners"][0]).values(status="0"))
        db.commit()
    before = snapshot(env)
    env["io"].clear()
    env["writes"].clear()
    response = upload(env, credential=f"Bearer {env['beta']['owner']}")
    assert response.json().get("retcode", response.json().get("code")) != 0
    assert not env["io"] and not env["writes"] and snapshot(env) == before


def test_download_uses_identity_and_uploaded_descriptor_not_created_by(agent_files: dict[str, Any]) -> None:
    env = agent_files
    descriptor = upload(env, credential=f"Bearer {env['jwts'][0]}").json()["data"]
    endpoint = f"{env['base']}/api/v1/agents/download"
    for credential in [env["jwts"][0], env["keys"][0]]:
        response = requests.get(endpoint, params={"id": descriptor["id"], "created_by": "forged"}, headers={"Authorization": f"Bearer {credential}"}, timeout=30)
        assert response.status_code == 200 and response.content == b"one"
        assert response.headers["X-Content-Type-Options"] == "nosniff" and response.headers["Cache-Control"] == "no-store"
    env["io"].clear()
    for credential in [None, "Bearer invalid", f"Bearer {env['beta']['owner']}"]:
        response = requests.get(endpoint, params={"id": descriptor["id"], "created_by": descriptor["created_by"]}, headers={"Authorization": credential} if credential else {}, timeout=30)
        assert response.status_code != 200 or response.json().get("code", response.json().get("retcode")) != 0
    assert not env["io"]
    response = requests.get(endpoint, params={"id": descriptor["id"], "created_by": descriptor["created_by"]}, headers={"Authorization": f"Bearer {env['jwts'][1]}"}, timeout=30)
    assert response.status_code == 404
    paths = requests.get(f"{env['base']}/openapi.json", timeout=30).json()["paths"]
    assert "created_by" not in {p["name"] for p in paths["/api/v1/agents/download"]["get"]["parameters"]}


def test_real_postgres_probe_closes_connections_and_private_policy_rejects(agent_files: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import agent_db_probe

    env = agent_files
    target = env["engine"].url
    payload = {"db_type": "postgres", "database": target.database, "username": target.username, "host": "127.0.0.1", "port": target.port or 5432, "password": target.password}
    endpoint = f"{env['base']}/api/v1/agents/test_db_connection"
    response = env["client"].post(endpoint, json=payload, timeout=30)
    assert response.json()["retcode"] != 0 and "public" in response.json()["retmsg"]
    # Controlled transport boundary for the positive lifecycle test only.
    # Production validation above remains real; no private-host config bypass.
    with monkeypatch.context() as patch:
        patch.setattr(agent_db_probe, "assert_host_is_safe", lambda _: target.host)
        for _ in range(3):
            response = env["client"].post(endpoint, json=payload, timeout=30)
            assert response.status_code == 200 and response.json()["retcode"] == 0
        bad = dict(payload, password="unique-secret-never-render")
        response = env["client"].post(endpoint, json=bad, timeout=30)
        assert response.json()["retcode"] != 0 and "unique-secret-never-render" not in response.text
    with env["engine"].connect() as db:
        count = db.scalar(sa.text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND application_name=''"))
        assert count == 0
    env["record"]["probe_connections_remaining"] = count


def test_uploaded_files_run_with_server_metadata_and_session_path_binding(agent_files: dict[str, Any]) -> None:
    env = agent_files
    descriptor = upload(env, credential=f"Bearer {env['beta']['owner']}").json()["data"]
    forged = {**descriptor, "created_by": env["owners"][1], "name": "forged.pdf", "mime_type": "image/png"}
    payload = {"query": "read upload", "files": [forged], "inputs": {"document": {"type": "file", "value": [forged]}}, "stream": False, "user_id": "external-display-user"}
    response = requests.post(f"{env['base']}/api/v1/agentbots/{env['agents']['private']}/completions", headers={"Authorization": f"Bearer {env['beta']['owner']}"}, json=payload, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0
    session_id = response.json()["data"]["session_id"]
    with Session(env["engine"]) as db:
        session = db.get(API4Conversation, session_id)
        assert session is not None and session.dialog_id == env["agents"]["private"] and session.source == "agent"
        assert session.user_id == "external-display-user"
        assert session.message[0]["files"] == [descriptor]
    before = snapshot(env)
    env["io"].clear()
    env["writes"].clear()
    tasks = len(env["task_ids"])
    # Cross-agent session IDs and foreign beta replay are rejected before any
    # file storage IO, SQL writes, runtime creation or scheduling.
    for agent, token in [("team", "owner"), ("private", "foreign")]:
        response = requests.post(
            f"{env['base']}/api/v1/agentbots/{env['agents'][agent]}/completions", headers={"Authorization": f"Bearer {env['beta'][token]}"}, json={**payload, "session_id": session_id}, timeout=30
        )
        assert response.json()["code"] != 0
    assert not env["io"] and not env["writes"] and snapshot(env) == before and len(env["task_ids"]) == tasks
    # A valid owner resume still uses the original path/session relationship.
    response = requests.post(
        f"{env['base']}/api/v1/agentbots/{env['agents']['private']}/completions", headers={"Authorization": f"Bearer {env['beta']['owner']}"}, json={**payload, "session_id": session_id}, timeout=30
    )
    assert response.json()["code"] == 0 and response.json()["data"]["session_id"] == session_id
    # The debug path consumes the same server-written descriptor, even when a
    # caller supplies forged metadata in the run request.
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json={"agent_id": env["agents"]["private"], **payload}, timeout=30)
    assert response.status_code == 200 and '"event": "error"' not in response.text
    assert response.json()["retcode"] == 0 and response.json()["data"]["data"]["content"] == "file run"


def test_foreign_uploaded_descriptor_denied_before_run_side_effects(agent_files: dict[str, Any]) -> None:
    env = agent_files
    descriptor = upload(env, agent="foreign", credential=f"Bearer {env['jwts'][1]}").json()["data"]
    before = snapshot(env)
    env["writes"].clear()
    tasks = len(env["task_ids"])
    response = requests.post(
        f"{env['base']}/api/v1/agentbots/{env['agents']['private']}/completions",
        headers={"Authorization": f"Bearer {env['beta']['owner']}"},
        json={"query": "forged", "stream": False, "inputs": {"file": {"type": "file", "value": descriptor}}},
        timeout=30,
    )
    assert response.json()["code"] != 0
    assert snapshot(env) == before and not env["writes"] and len(env["task_ids"]) == tasks
