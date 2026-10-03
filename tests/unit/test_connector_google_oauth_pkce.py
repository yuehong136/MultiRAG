"""Real Google Flow/OAuthLib encoding with an isolated cache and token transport."""

import base64
import hashlib
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from api.apps.services import connector_oauth_service as oauth
from common.constants import RetCode
from common.data_source.config import DocumentSource
from common.data_source.google_util.auth import get_google_oauth_creds
from common.data_source.google_util.constant import GOOGLE_SCOPES

CLIENT_CONFIG = {"web": {"client_id": "scratch-client", "client_secret": "scratch-secret", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def set_obj(self, key: str, value: Any, exp: int) -> bool:
        self.values[key] = json.dumps(value)
        self.ttls[key] = exp
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest.fixture
def cache(monkeypatch: pytest.MonkeyPatch) -> MemoryCache:
    cache = MemoryCache()
    monkeypatch.setattr(oauth, "REDIS_CONN", cache)
    return cache


def challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


@pytest.mark.parametrize("source", ["gmail", "google-drive"])
def test_pkce_roundtrip_actual_flow_and_encoded_token_request(cache: MemoryCache, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    redirect = f"https://multirag.example/v1/connector/{source}/oauth/web/callback"
    ok, start, _ = oauth.start_google_oauth("owner", source, CLIENT_CONFIG, redirect)
    assert ok
    query = parse_qs(urlsplit(start["authorization_url"]).query)
    state_key = oauth.web_state_cache_key(start["flow_id"], source)
    state = json.loads(cache.get(state_key))
    verifier = state["code_verifier"]
    expected_scopes = GOOGLE_SCOPES[DocumentSource.GMAIL if source == "gmail" else DocumentSource.GOOGLE_DRIVE]
    assert 43 <= len(verifier) <= 128
    assert query["code_challenge"] == [challenge(verifier)] and query["code_challenge_method"] == ["S256"]
    assert verifier not in start["authorization_url"] and "code_verifier" not in start
    assert query["state"] == [start["flow_id"]] and query["redirect_uri"] == [redirect]
    assert query["scope"][0].split() == expected_scopes
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"] and query["include_granted_scopes"] == ["true"]
    assert state["user_id"] == "owner" and cache.ttls[state_key] == oauth.WEB_FLOW_TTL_SECS
    requests_seen: list[dict[str, list[str]]] = []

    def send(session: requests.Session, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        assert request.url == CLIENT_CONFIG["web"]["token_uri"] and request.method == "POST"
        body = parse_qs(request.body)
        assert body["code_verifier"] == [verifier] and body["code"] == ["scratch-code"]
        assert body["redirect_uri"] == [redirect] and body["grant_type"] == ["authorization_code"]
        requests_seen.append(body)
        response = requests.Response()
        response.request = request
        response.status_code = 200
        response._content = json.dumps({"access_token": "scratch-token", "refresh_token": "scratch-refresh", "token_type": "Bearer", "expires_in": 3600, "scope": " ".join(expected_scopes)}).encode()
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    assert oauth.handle_google_callback(source, start["flow_id"], "scratch-code", None, None)[1]
    assert len(requests_seen) == 1 and cache.get(state_key) is None
    result_key = oauth.web_result_cache_key(start["flow_id"], source)
    assert cache.ttls[result_key] == oauth.WEB_FLOW_TTL_SECS
    assert oauth.poll_google_result("other-tenant-user", source, start["flow_id"])[2] == RetCode.PERMISSION_ERROR
    assert cache.get(result_key) is not None
    ok, result, _ = oauth.poll_google_result("owner", source, start["flow_id"])
    assert ok and cache.get(result_key) is None and verifier not in result["credentials"]
    credentials = get_google_oauth_creds(result["credentials"], DocumentSource.GMAIL if source == "gmail" else DocumentSource.GOOGLE_DRIVE)
    assert credentials and credentials.refresh_token == "scratch-refresh" and credentials.valid
    assert oauth.poll_google_result("owner", source, start["flow_id"])[2] == RetCode.RUNNING
    assert not oauth.handle_google_callback(source, start["flow_id"], "scratch-code", None, None)[1]
    assert len(requests_seen) == 1


@pytest.mark.parametrize("cached_verifier", [None, "saved-verifier"])
def test_cached_verifier_compatibility(cache: MemoryCache, monkeypatch: pytest.MonkeyPatch, cached_verifier: str | None) -> None:
    state = {"user_id": "owner", "client_config": CLIENT_CONFIG}
    if cached_verifier is not None:
        state["code_verifier"] = cached_verifier
    cache.set_obj(oauth.web_state_cache_key("old-flow", "gmail"), state, 900)
    seen: list[dict[str, list[str]]] = []

    def send(session: requests.Session, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        body = parse_qs(request.body)
        assert body.get("code_verifier") == ([cached_verifier] if cached_verifier else None)
        seen.append(body)
        response = requests.Response()
        response.request = request
        response.status_code = 400
        response._content = b'{"error":"invalid_grant"}'
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    assert not oauth.handle_google_callback("gmail", "old-flow", "code", None, None)[1]
    assert len(seen) == 1 and cache.get(oauth.web_state_cache_key("old-flow", "gmail")) is None
    assert cache.get(oauth.web_result_cache_key("old-flow", "gmail")) is None


@pytest.mark.parametrize("source", ["gmail", "google-drive"])
def test_state_source_and_failure_lifecycle(cache: MemoryCache, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    def unexpected_send(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid callbacks must not exchange tokens")

    monkeypatch.setattr(requests.Session, "send", unexpected_send)
    assert not oauth.handle_google_callback(source, None, "code", None, None)[1]
    assert not oauth.handle_google_callback(source, "expired", "code", None, None)[1]
    ok, start, _ = oauth.start_google_oauth("owner", source, CLIENT_CONFIG, None)
    assert ok
    flow_id = start["flow_id"]
    key = oauth.web_state_cache_key(flow_id, source)
    other_source = "google-drive" if source == "gmail" else "gmail"
    assert not oauth.handle_google_callback(other_source, flow_id, "code", None, None)[1] and cache.get(key)
    assert not oauth.handle_google_callback(source, flow_id, None, None, None)[1] and cache.get(key)
    assert not oauth.handle_google_callback(source, flow_id, "code", "access_denied", "Cancelled")[1]
    assert cache.get(key) is None and cache.get(oauth.web_result_cache_key(flow_id, source)) is None
