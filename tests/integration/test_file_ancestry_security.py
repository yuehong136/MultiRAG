"""Owned HTTP ancestry/auth probes with independent PostgreSQL readback."""

import base64
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from itsdangerous import TimestampSigner
from sqlalchemy.orm import Session

from api.db.db_models import Document, File, File2Document, Knowledgebase, User, UserTenant
from common import settings
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


def _evidence(name: str, payload: dict[str, Any]) -> None:
    if directory := os.environ.get("MULTIRAG_ANCESTRY_EVIDENCE_DIR"):
        path = Path(directory) / name
        path.write_text(json.dumps(payload, default=str, indent=2))
        path.chmod(0o600)


def _files(engine: sa.Engine) -> list[dict[str, Any]]:
    with engine.connect() as db:
        return [dict(row) for row in db.execute(sa.select(File.__table__).order_by(File.id)).mappings()]


def _cookie(user_id: str) -> dict[str, str]:
    encoded = base64.b64encode(json.dumps({"_user_id": user_id}).encode())
    return {"multirag_session": TimestampSigner(settings.SECRET_KEY).sign(encoded).decode()}


def test_file_ancestry_authorization_and_readback(runtime_upload_api: dict[str, Any]) -> None:
    from api.apps import manager

    env = runtime_upload_api
    owner, other = env["owners"]
    ids = {name: uuid4().hex for name in ("root", "folder", "leaf", "kb", "doc", "link", "membership")}
    tokens = {"owner": env["jwt"], "other": manager.create_access_token(data={"sub": f"{other}@upload.test"}), "api-key": env["api_key"]}
    records: list[dict[str, Any]] = []
    try:
        with Session(env["engine"]) as db:
            for name, parent in (("root", "root"), ("folder", "root"), ("leaf", "folder")):
                db.add(File(id=ids[name], parent_id=ids[parent], tenant_id=owner, created_by=owner, name="secret-" + name, type="doc" if name == "leaf" else "folder"))
            db.commit()
        before = _files(env["engine"])
        for suffix, key, expected in (("parent", "parent_folder", [ids["folder"]]), ("ancestors", "parent_folders", [ids["leaf"], ids["folder"], ids["root"]])):
            route = f"{env['base']}/api/v1/files/{ids['leaf']}/{suffix}"
            allowed = requests.get(route, headers={"Authorization": "Bearer " + tokens["owner"]}, cookies=_cookie(other), timeout=30)
            body = allowed.json()
            assert allowed.status_code == 200 and body["code"] == 0, body
            rows = [body["data"][key]] if suffix == "parent" else body["data"][key]
            assert [row["id"] for row in rows] == expected
            for actor in ("other", "api-key"):
                denied = requests.get(route, headers={"Authorization": "Bearer " + tokens[actor]}, cookies=_cookie(owner), params={"user_id": owner, "tenant_id": owner}, timeout=30)
                payload = denied.json()
                assert denied.status_code == 200 and payload["code"] == 102, payload
                assert "secret-" not in denied.text and ids["folder"] not in denied.text
                records.append({"route": suffix, "actor": actor, "body": payload})
        assert _files(env["engine"]) == before
        # A superuser does not gain generic access to another user's file tree.
        with Session(env["engine"]) as db:
            db.execute(sa.update(User).where(User.id == other).values(is_superuser=True))
            db.commit()
        denied_admin = requests.get(f"{env['base']}/api/v1/files/{ids['leaf']}/ancestors", headers={"Authorization": "Bearer " + tokens["other"]}, timeout=30).json()
        assert denied_admin["code"] == 102
        assert _files(env["engine"]) == before
        # Preserve the existing team-share policy rather than widening it to
        # every tenant member or narrowing all reads to the file owner.
        with Session(env["engine"]) as db:
            db.execute(sa.update(User).where(User.id == other).values(is_superuser=False))
            db.add(Knowledgebase(id=ids["kb"], tenant_id=owner, created_by=owner, name="Shared scratch", permission="team", parser_id="naive", embd_id="scratch"))
            db.add(Document(id=ids["doc"], kb_id=ids["kb"], created_by=owner, name="shared", parser_id="naive", type="doc", location=""))
            db.add(File2Document(id=ids["link"], file_id=ids["leaf"], document_id=ids["doc"]))
            db.add(UserTenant(id=ids["membership"], user_id=other, tenant_id=owner, invited_by=owner, role="normal"))
            db.commit()
        shared = requests.get(f"{env['base']}/api/v1/files/{ids['leaf']}/ancestors", headers={"Authorization": "Bearer " + tokens["other"]}, timeout=30).json()
        assert shared["code"] == 0, shared
        with Session(env["engine"]) as db:
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["kb"]).values(permission="me"))
            db.commit()
        private = requests.get(f"{env['base']}/api/v1/files/{ids['leaf']}/ancestors", headers={"Authorization": "Bearer " + tokens["other"]}, timeout=30).json()
        assert private["code"] == 102
        assert _files(env["engine"]) == before
        for legacy in ("/v1/file/parent_folder", "/v1/file/all_parent_folder"):
            assert requests.get(env["base"] + legacy, params={"file_id": ids["leaf"]}, timeout=30).status_code == 404
        _evidence(
            "ancestry-readback.json",
            {"database": env["engine"].url.database, "file_ids": ids, "denials": records, "admin": denied_admin, "team": shared, "private": private, "unchanged_file_rows": before},
        )
    finally:
        with Session(env["engine"]) as db:
            for model, column, values in (
                (File2Document, File2Document.id, [ids["link"]]),
                (Document, Document.id, [ids["doc"]]),
                (Knowledgebase, Knowledgebase.id, [ids["kb"]]),
                (UserTenant, UserTenant.id, [ids["membership"]]),
                (File, File.id, [ids[n] for n in ("root", "folder", "leaf")]),
            ):
                db.execute(sa.delete(model).where(column.in_(values)))
            db.commit()


def test_header_cookie_priority_disable_and_logout(runtime_upload_api: dict[str, Any]) -> None:
    from api.apps import manager

    env = runtime_upload_api
    owner, other = env["owners"]
    route = env["base"] + "/api/v1/users/me"
    owner_cookie = _cookie(owner)
    headers = {"Authorization": "Bearer " + env["jwt"]}
    for auth in (None, "Bearer", "Bearer junk", "Bearer " + manager.create_access_token(data={"sub": "missing@upload.test"})):
        response = requests.get(route, headers={"Authorization": auth} if auth else {}, cookies=owner_cookie, timeout=30)
        assert response.status_code == 401, response.text
    success = requests.get(route, headers=headers, cookies=_cookie(other), timeout=30)
    assert success.status_code == 200 and success.json()["data"]["id"] == owner
    sdk = requests.get(route, headers={"Authorization": "Bearer " + env["api_key"]}, cookies=owner_cookie, timeout=30)
    assert sdk.status_code == 200 and sdk.json()["data"]["id"] == other
    with Session(env["engine"]) as db:
        db.execute(sa.update(User).where(User.id == owner).values(is_active=False))
        db.commit()
    assert requests.get(route, headers=headers, cookies=owner_cookie, timeout=30).status_code == 401
    with Session(env["engine"]) as db:
        db.execute(sa.update(User).where(User.id == owner).values(is_active=True))
        db.commit()
    logout = requests.post(env["base"] + "/api/v1/auth/logout", headers=headers, cookies=owner_cookie, timeout=30)
    assert logout.status_code == 200 and logout.json()["retcode"] == 0, logout.text
    with Session(env["engine"]) as db:
        assert db.get(User, owner).access_token.startswith("INVALID_")
    replay = requests.get(route, headers=headers, cookies=owner_cookie, timeout=30)
    assert replay.status_code == 401
    assert requests.get(route, cookies=owner_cookie, timeout=30).status_code == 401
    _evidence(
        "auth-readback.json",
        {
            "database": env["engine"].url.database,
            "header_owner": success.json()["data"]["id"],
            "api_key_owner": sdk.json()["data"]["id"],
            "logout": logout.json(),
            "sql_access_token_revoked": True,
            "replay_status": replay.status_code,
            "cookie_authentication": "not accepted",
        },
    )


@pytest.mark.parametrize("channel", ["oauth", "oidc"])
def test_oauth_redirect_does_not_create_unbound_session(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, channel: str) -> None:
    env = runtime_upload_api
    module = sys.modules["api.apps.restful_apis.user"]
    calls: list[str] = []

    async def exchange(code: str) -> dict[str, str]:
        calls.append("exchange")
        return {"access_token": "controlled-provider-token"}

    async def user_info(token: str, *, id_token: str | None = None) -> Any:
        calls.append("userinfo")
        return SimpleNamespace(email=f"{env['owners'][0]}@upload.test")

    monkeypatch.setattr(settings, "OAUTH_CONFIG", {channel: {"type": channel}})
    monkeypatch.setattr(
        module,
        "get_auth_client",
        lambda cfg: SimpleNamespace(get_authorization_url=lambda state: "https://idp.invalid/authorize?state=" + state, async_exchange_code_for_token=exchange, async_fetch_user_info=user_info),
    )
    with Session(env["engine"]) as db:
        before = [(u.id, u.access_token, u.last_login_time) for u in db.scalars(sa.select(User).order_by(User.id))]
    with requests.Session() as client:
        start = client.get(env["base"] + "/api/v1/auth/login/" + channel, allow_redirects=False, timeout=30)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        callback = env["base"] + f"/api/v1/auth/oauth/{channel}/callback"
        denied = client.get(callback, params={"code": "controlled-code", "state": state}, allow_redirects=False, timeout=30)
        assert denied.headers["location"] == "/?error=oauth_identity_binding_required"
        assert "Authorization" not in denied.headers and calls == ["exchange", "userinfo"]
        replay = client.get(callback, params={"code": "controlled-code", "state": state}, allow_redirects=False, timeout=30)
        assert replay.headers["location"] == "/?error=invalid_state"
        assert client.get(env["base"] + "/api/v1/users/me", timeout=30).status_code == 401
    with Session(env["engine"]) as db:
        after = [(u.id, u.access_token, u.last_login_time) for u in db.scalars(sa.select(User).order_by(User.id))]
    assert before == after
    _evidence(channel + "-readback.json", {"database": env["engine"].url.database, "redirect": denied.headers["location"], "replay": replay.headers["location"], "identity_rows_unchanged": True})


def test_health_http_status_and_public_projection(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.utils import health_utils

    base = runtime_upload_api["base"]
    assert requests.get(base + "/api/v1/system/ping", timeout=30).status_code == 200
    assert requests.get(base + "/v1/system/healthz", timeout=30).status_code == 404
    real = requests.get(base + "/api/v1/system/healthz", timeout=30)
    assert real.status_code == (200 if real.json()["status"] == "ok" else 500)
    assert "_meta" not in real.json()

    async def db_failure(db: Any) -> tuple[bool, dict[str, Any]]:
        return False, {"error": "private-config-and-credentials"}

    monkeypatch.setattr(health_utils, "check_db_async", db_failure)
    failure = requests.get(base + "/api/v1/system/healthz", timeout=30)
    assert failure.status_code == 500 and failure.json()["db"] == "nok"
    assert "private-config" not in failure.text and "_meta" not in failure.json()
    _evidence("health-http.json", {"real_status": real.status_code, "real_body": real.json(), "fault_status": failure.status_code, "fault_body": failure.json(), "legacy_status": 404})
