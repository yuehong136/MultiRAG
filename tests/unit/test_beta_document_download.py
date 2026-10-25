"""Beta download route rejects ambiguous identity before resource/storage lookup."""

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from api.db.db_models import APIToken


def test_unique_beta_token_precedes_document_resolution(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.restful_apis.document_download"]
    monkeypatch.setattr(APIToken, "query", lambda *args, **kwargs: [SimpleNamespace(tenant_id="a"), SimpleNamespace(tenant_id="b")])

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ambiguous identity reached a resource")

    monkeypatch.setattr(module.Tenant, "query", forbidden)
    monkeypatch.setattr(module.DocumentService, "query", forbidden)
    monkeypatch.setattr(module.File2DocumentService, "get_authorized_document_storage_address", forbidden)
    response = client.get("/api/v1/documents/doc", headers={"Authorization": "Bearer synthetic-beta"})
    assert response.status_code == 200 and response.json()["code"] != 0
    assert response.json()["message"] == "Authentication error: API key configuration is ambiguous."


@pytest.mark.parametrize("header", ["", "Bearer", "Basic synthetic-beta", "Bearer synthetic-beta extra"])
def test_malformed_auth_never_looks_up_tokens(client: Any, monkeypatch: pytest.MonkeyPatch, header: str) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("malformed auth reached token lookup")

    monkeypatch.setattr(APIToken, "query", forbidden)
    response = client.get("/api/v1/documents/doc", headers={"Authorization": header})
    assert response.status_code == 200 and response.json()["code"] != 0
