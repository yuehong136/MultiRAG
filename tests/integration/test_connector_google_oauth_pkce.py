"""Scratch HTTP/auth/Redis OAuth roundtrip; only Google is a loopback token server."""

import base64
import hashlib
import json
import os
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from sqlalchemy.orm import Session, sessionmaker

from api.apps.services import connector_oauth_service as oauth
from api.db.db_models import get_db
from common.constants import RetCode
from common.data_source.config import DocumentSource
from common.data_source.google_util.auth import get_google_oauth_creds
from common.data_source.google_util.constant import GOOGLE_SCOPES
from tests.integration.test_runtime_document_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def oauth_api(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    from api import apps

    env = runtime_upload_api
    sessions = sessionmaker(env["engine"], expire_on_commit=False)

    def scratch_db() -> Iterator[Session]:
        with sessions() as db:
            yield db

    monkeypatch.setattr(apps, "SessionLocal", sessions)
    monkeypatch.setitem(apps.app.dependency_overrides, get_db, scratch_db)
    # Local fake provider uses HTTP. This is confined to the test process.
    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
    exchanges: list[dict[str, list[str]]] = []
    expected: dict[str, Any] = {}

    class TokenServer(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            exchanges.append(body)
            verifier = body.get("code_verifier", [""])[0]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            valid = (
                self.path == "/token"
                and challenge == expected.get("challenge")
                and body.get("redirect_uri") == [expected.get("redirect")]
                and body.get("code") == ["scratch-code"]
                and body.get("grant_type") == ["authorization_code"]
            )
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = (
                {"access_token": "scratch-token", "refresh_token": "scratch-refresh", "expires_in": 3600, "token_type": "Bearer", "scope": expected["scope"]} if valid else {"error": "invalid_grant"}
            )
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), TokenServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env.update(token_uri=f"http://127.0.0.1:{server.server_port}/token", exchanges=exchanges, expected=expected, oauth_keys=[])
    try:
        assert oauth.REDIS_CONN.REDIS.ping()
        yield env
    finally:
        if env["oauth_keys"]:
            oauth.REDIS_CONN.REDIS.delete(*env["oauth_keys"])
            assert not any(oauth.REDIS_CONN.REDIS.exists(key) for key in env["oauth_keys"])
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("source", ["gmail", "google-drive"])
@pytest.mark.parametrize("prefix", ["/api/v1/connectors", "/v1/connector"])
def test_real_http_google_pkce_state_and_credentials(oauth_api: dict[str, Any], source: str, prefix: str) -> None:
    from api import apps

    env = oauth_api
    base = env["base"]
    if prefix == "/api/v1/connectors" and source == "gmail":
        smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": base}, capture_output=True, text=True, timeout=60)
        env["record_path"].with_suffix(".smoke.log").write_text(smoke.stdout + smoke.stderr + f"\nexit={smoke.returncode}\n")
        assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    credentials = {"web": {"client_id": "scratch-client", "client_secret": "scratch-secret", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": env["token_uri"]}}
    start_url = f"{base}{prefix}/google/oauth/web/start"
    redirect = f"{base}{prefix}/{source}/oauth/web/callback"
    assert requests.post(start_url, params={"source": source}, json={"credentials": credentials}, timeout=10).status_code == 401
    started = requests.post(start_url, headers=headers, params={"source": source}, json={"credentials": credentials, "redirect_uri": redirect}, timeout=10)
    assert started.status_code == 200 and started.json()["retcode"] == 0
    data = started.json()["data"]
    state = data["flow_id"]
    state_key = oauth.web_state_cache_key(state, source)
    result_key = oauth.web_result_cache_key(state, source)
    env["oauth_keys"].extend([state_key, result_key])
    cache = oauth.REDIS_CONN.REDIS
    cached = json.loads(cache.get(state_key))
    query = parse_qs(urlsplit(data["authorization_url"]).query)
    verifier = cached["code_verifier"]
    assert cached["user_id"] == env["owners"][0] and cached["redirect_uri"] == redirect
    assert query["state"] == [state] and query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")]
    assert verifier not in started.text and 0 < cache.ttl(state_key) <= oauth.WEB_FLOW_TTL_SECS
    scopes = GOOGLE_SCOPES[DocumentSource.GMAIL if source == "gmail" else DocumentSource.GOOGLE_DRIVE]
    assert query["scope"][0].split() == scopes
    env["expected"].update(challenge=query["code_challenge"][0], redirect=redirect, scope=" ".join(scopes))
    result_url = f"{base}{prefix}/google/oauth/web/result"
    pending = requests.post(result_url, headers=headers, params={"source": source}, json={"flow_id": state}, timeout=10)
    assert pending.json()["retcode"] == RetCode.RUNNING
    wrong_source = "gmail" if source == "google-drive" else "google-drive"
    wrong = requests.get(f"{base}{prefix}/{wrong_source}/oauth/web/callback", params={"state": state, "code": "scratch-code"}, timeout=10)
    assert wrong.status_code == 200 and '"status": "error"' in wrong.text
    assert cache.get(state_key) and not env["exchanges"]
    callback = requests.get(redirect, params={"state": state, "code": "scratch-code"}, timeout=10)
    assert callback.status_code == 200 and '"status": "success"' in callback.text
    assert verifier not in callback.text and len(env["exchanges"]) == 1
    assert env["exchanges"][0]["code_verifier"] == [verifier]
    assert not cache.exists(state_key) and 0 < cache.ttl(result_key) <= oauth.WEB_FLOW_TTL_SECS
    other_token = apps.manager.create_access_token(data={"sub": f"{env['owners'][1]}@upload.test"})
    denied = requests.post(result_url, headers={"Authorization": f"Bearer {other_token}"}, params={"source": source}, json={"flow_id": state}, timeout=10)
    assert denied.status_code == 200 and denied.json()["retcode"] == RetCode.PERMISSION_ERROR and cache.exists(result_key)
    result = requests.post(result_url, headers=headers, params={"source": source}, json={"flow_id": state}, timeout=10)
    assert result.status_code == 200 and result.json()["retcode"] == 0 and not cache.exists(result_key)
    restored = get_google_oauth_creds(result.json()["data"]["credentials"], DocumentSource.GMAIL if source == "gmail" else DocumentSource.GOOGLE_DRIVE)
    assert restored and restored.valid and restored.refresh_token == "scratch-refresh" and restored.scopes == scopes
    replay = requests.get(redirect, params={"state": state, "code": "scratch-code"}, timeout=10)
    assert '"status": "error"' in replay.text and len(env["exchanges"]) == 1


@pytest.mark.parametrize("source", ["gmail", "google-drive"])
def test_real_http_google_callback_failure_cleanup(oauth_api: dict[str, Any], source: str) -> None:
    env = oauth_api
    base = env["base"] + "/api/v1/connectors"
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    credentials = {"web": {"client_id": "scratch-client", "client_secret": "scratch-secret", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": env["token_uri"]}}
    cache = oauth.REDIS_CONN.REDIS
    for failure in ["token", "cancel", "expired"]:
        started = requests.post(f"{base}/google/oauth/web/start", headers=headers, params={"source": source}, json={"credentials": credentials}, timeout=10)
        assert started.json()["retcode"] == 0
        state = started.json()["data"]["flow_id"]
        keys = [oauth.web_state_cache_key(state, source), oauth.web_result_cache_key(state, source)]
        env["oauth_keys"].extend(keys)
        params = {"state": state, "code": "rejected-code"}
        if failure == "cancel":
            params.update(error="access_denied", error_description="Cancelled")
        if failure == "expired":
            cache.delete(keys[0])
        before = len(env["exchanges"])
        env["expected"]["scope"] = ""
        response = requests.get(f"{base}/{source}/oauth/web/callback", params=params, timeout=10)
        assert response.status_code == 200 and '"status": "error"' in response.text
        assert not any(cache.exists(key) for key in keys)
        assert len(env["exchanges"]) == before + (1 if failure == "token" else 0)
