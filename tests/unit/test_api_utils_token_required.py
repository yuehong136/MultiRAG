from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session
from starlette.requests import Request

from api.utils.api_utils import SDKAuthError, token_required


def _build_request(authorization: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("utf-8")))
    return Request({"type": "http", "headers": headers})


def _build_session() -> Session:
    return Session()


def test_token_required_rejects_missing_authorization():
    request = _build_request()
    db = _build_session()

    with pytest.raises(SDKAuthError) as exc_info:
        token_required(request, db=db)

    assert exc_info.value.retcode == 109
    assert exc_info.value.retmsg == "`Authorization` can't be empty"
    db.close()


def test_token_required_rejects_invalid_api_key(monkeypatch):
    request = _build_request("Bearer invalid-token")
    db = _build_session()
    monkeypatch.setattr("api.utils.api_utils.APIToken.query", lambda db, token: [])

    with pytest.raises(SDKAuthError) as exc_info:
        token_required(request, db=db)

    assert exc_info.value.retcode == 109
    assert exc_info.value.retmsg == "Authentication error: API key is invalid!"
    db.close()


def test_token_required_returns_tenant_id(monkeypatch):
    request = _build_request("Bearer valid-token")
    db = _build_session()
    monkeypatch.setattr(
        "api.utils.api_utils.APIToken.query",
        lambda db, token: [SimpleNamespace(tenant_id="tenant-1")],
    )

    assert token_required(request, db=db) == "tenant-1"
    db.close()
