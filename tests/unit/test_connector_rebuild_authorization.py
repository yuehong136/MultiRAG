"""Rebuild authorization must precede every destructive service boundary."""

from typing import NoReturn

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from api.db.services.connector_service import Connector2KbService, ConnectorAuthorizationError, ConnectorService, SyncLogsService
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.constants import RetCode


@pytest.mark.parametrize(("connector_access", "kb_access", "bound"), [(False, True, True), (True, False, True), (True, True, False)])
def test_rebuild_denies_before_any_destructive_call(db: Session, monkeypatch: pytest.MonkeyPatch, connector_access: bool, kb_access: bool, bound: bool) -> None:
    monkeypatch.setattr(ConnectorService, "accessible", classmethod(lambda cls, *args: connector_access))
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, *args: kb_access))
    monkeypatch.setattr(Connector2KbService, "query", classmethod(lambda cls, *args, **kwargs: [object()] if bound else []))
    touched: list[str] = []
    for service, method in [(SyncLogsService, "filter_delete"), (SyncLogsService, "schedule"), (DocumentService, "query"), (FileService, "delete_docs")]:
        monkeypatch.setattr(service, method, classmethod(lambda cls, *args, _method=method, **kwargs: touched.append(_method)))

    with pytest.raises(ConnectorAuthorizationError, match=r"^No authorization\.$"):
        ConnectorService.rebuild(db, "connector", "dataset", "caller")

    assert touched == []


@pytest.mark.parametrize(("method", "path"), [("POST", "/api/v1/connectors/connector/rebuild"), ("PUT", "/v1/connector/connector/rebuild")])
def test_rebuild_maps_service_denial_to_generic_auth_envelope(client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str) -> None:
    monkeypatch.setattr(ConnectorService, "accessible", classmethod(lambda cls, *args: True))

    def denied(cls: type[ConnectorService], db: Session, connector_id: str, kb_id: str, tenant_id: str) -> NoReturn:
        raise ConnectorAuthorizationError("internal binding details must stay private")

    monkeypatch.setattr(ConnectorService, "rebuild", classmethod(denied))
    response = client.request(method, path, json={"kb_id": "dataset"})

    assert response.status_code == 200
    assert response.json() == {"retcode": RetCode.AUTHENTICATION_ERROR, "retmsg": "No authorization.", "data": False}
