"""Real REST envelope for absent details and service failures."""

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from api.db.services.search_service import SearchService
from api.db.services.user_service import UserTenantService
from common.constants import RetCode


def test_missing_detail_after_scope_check(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(UserTenantService, "query", classmethod(lambda *a, **k: [SimpleNamespace(tenant_id="tenant-unit")]))
    monkeypatch.setattr(SearchService, "query", classmethod(lambda *a, **k: [SimpleNamespace(id="s1")]))
    monkeypatch.setattr(SearchService, "get_detail", classmethod(lambda *a: {}))
    response = client.get("/api/v1/searches/s1")
    assert response.status_code == 200
    assert response.json() == {"code": int(RetCode.DATA_ERROR), "message": "Can't find this Search App!"}


def test_detail_database_error_is_not_not_found(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(UserTenantService, "query", classmethod(lambda *a, **k: [SimpleNamespace(tenant_id="tenant-unit")]))
    monkeypatch.setattr(SearchService, "query", classmethod(lambda *a, **k: [SimpleNamespace(id="s1")]))

    def fail(*args: Any) -> None:
        raise SQLAlchemyError("controlled failure")

    monkeypatch.setattr(SearchService, "get_detail", fail)
    response = client.get("/api/v1/searches/s1")
    assert response.status_code == 200
    assert response.json()["code"] == int(RetCode.DATA_ERROR)
    assert response.json()["message"] == "Internal server error"
