"""Authenticated SQLAdmin writes cannot bypass mapping lifecycle services."""

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy.pool import StaticPool
from starlette.applications import Starlette
from starlette.requests import Request

from api.admin.admin_config import _BeartypeTolerantAdmin
from api.admin.admin_models import File2DocumentAdmin
from tests.support.resource_journal import ResourceJournal


class _ControlledAdminAuth(AuthenticationBackend):
    """Keep SQLAdmin's real signed session with a controlled login boundary."""

    def __init__(self) -> None:
        super().__init__(secret_key="file-mapping-admin-test-signer")
        self.checked: list[tuple[str, bool]] = []

    async def login(self, request: Request) -> bool:
        request.session["verified_admin"] = True
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        authenticated = request.session.get("verified_admin") is True
        self.checked.append((request.url.path, authenticated))
        return authenticated


@pytest.fixture
def mapping_admin() -> Iterator[tuple[TestClient, _ControlledAdminAuth, str, list[str]]]:
    journal = ResourceJournal("file-mapping-admin-readonly")
    journal.event("reserve-memory-engine", url="sqlite://", backing_file=None, lifecycle="dispose owned engine after controlled HTTP requests")
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    statements: list[str] = []

    def forbid_sql(connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        statements.append(statement)
        raise AssertionError("A rejected admin mapping write reached SQL")

    sa.event.listen(engine, "before_cursor_execute", forbid_sql)
    app, auth = Starlette(), _ControlledAdminAuth()
    admin = _BeartypeTolerantAdmin(app=app, engine=engine, authentication_backend=auth)
    admin.add_view(File2DocumentAdmin)
    identity = admin.views[0].identity
    try:
        with TestClient(app) as client:
            yield client, auth, identity, statements
    finally:
        engine.dispose()
        journal.event("memory-engine-cleanup", disposed=True, sql_count=len(statements), external_resources_created=False)


def test_file_mapping_admin_disables_all_write_actions() -> None:
    assert File2DocumentAdmin.can_create is False
    assert File2DocumentAdmin.can_edit is False
    assert File2DocumentAdmin.can_delete is False


@pytest.mark.parametrize(("method", "operation"), [("GET", "create"), ("POST", "create"), ("GET", "edit/mapping"), ("POST", "edit/mapping"), ("DELETE", "delete?pks=mapping")])
def test_authenticated_mapping_admin_write_denied_before_sql(mapping_admin: tuple[TestClient, _ControlledAdminAuth, str, list[str]], method: str, operation: str) -> None:
    client, auth, identity, statements = mapping_admin
    path = f"/admin/{identity}/{operation}"
    payload = {"id": "mapping", "file_id": "file", "document_id": "document"} if method == "POST" else None
    anonymous = client.request(method, path, data=payload, follow_redirects=False)
    assert anonymous.status_code == 302 and anonymous.headers["location"].endswith("/admin/login")
    assert auth.checked[-1][1] is False
    login = client.post("/admin/login", data={"username": "verified-admin"}, follow_redirects=False)
    assert login.status_code == 302 and "session" in client.cookies
    denied = client.request(method, path, data=payload, follow_redirects=False)
    assert denied.status_code == 403 and "location" not in denied.headers
    assert auth.checked[-1] == (path.partition("?")[0], True)
    assert statements == []
