import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from infinity.common import InfinityException
from infinity.errors import ErrorCode
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps import document_app
from api.apps.services import document_api_service
from api.db.db_models import Document, Knowledgebase
from api.db.services.document_status_service import STATUS_ERROR
from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, TenantMembershipEvidence, build_principal_from_authenticated_actor
from common.constants import RetCode
from core.utils.infinity_conn import InfinityConnection as DocumentInfinityConnection
from memory.utils.infinity_conn import InfinityConnection as MemoryInfinityConnection


class FakeColumns:
    def rows(self) -> list[tuple[Any, ...]]:
        return []


class FakeTable:
    def __init__(self, error: InfinityException | None = None) -> None:
        self.error = error
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def show_columns(self) -> FakeColumns:
        return FakeColumns()

    def update(self, condition: str, values: dict[str, Any]) -> SimpleNamespace:
        if self.error:
            raise self.error
        self.updates.append((condition, values))
        return SimpleNamespace(error_code=ErrorCode.OK)


class FakeDatabase:
    def __init__(self, table: FakeTable, error: InfinityException | None = None) -> None:
        self.table = table
        self.error = error

    def get_table(self, name: str) -> FakeTable:
        if self.error:
            raise self.error
        return self.table


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database

    def get_database(self, name: str) -> FakeDatabase:
        return self.database


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.release_count = 0

    def get_conn(self) -> FakeConnection:
        return self.connection

    def release_conn(self, connection: FakeConnection) -> None:
        assert connection is self.connection
        self.release_count += 1


def make_store(factory: Callable[..., Any], database: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FakePool]:
    cls = next(cell.cell_contents for cell in factory.__closure__ if isinstance(cell.cell_contents, type))
    store = object.__new__(cls)
    pool = FakePool(FakeConnection(database))
    store.connPool = pool
    store.dbName = "scratch"
    store.logger = logging.getLogger("test.infinity.update")

    def fixed_filter(condition: dict[str, Any], table: FakeTable) -> str:
        return "id = 'row'"

    monkeypatch.setattr(store, "equivalent_condition_to_str", fixed_filter)
    return store, pool


@pytest.mark.parametrize("factory", [DocumentInfinityConnection, MemoryInfinityConnection])
@pytest.mark.parametrize("phase", ["lookup", "write"])
def test_table_not_exist_returns_false_and_releases_connection(factory: Callable[..., Any], phase: str, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = InfinityException(ErrorCode.TABLE_NOT_EXIST, "missing table")
    table = FakeTable(missing if phase == "write" else None)
    database = FakeDatabase(table, missing if phase == "lookup" else None)
    store, pool = make_store(factory, database, monkeypatch)
    values = {"available_int": 0} if factory is DocumentInfinityConnection else {"status": 0}

    assert store.update({"id": "row"}, values, "index", "dataset") is False
    assert pool.release_count == 1


@pytest.mark.parametrize("factory", [DocumentInfinityConnection, MemoryInfinityConnection])
@pytest.mark.parametrize("phase", ["lookup", "write"])
def test_other_infinity_errors_propagate(factory: Callable[..., Any], phase: str, monkeypatch: pytest.MonkeyPatch) -> None:
    failure = InfinityException(3999, "update failed")
    table = FakeTable(failure if phase == "write" else None)
    database = FakeDatabase(table, failure if phase == "lookup" else None)
    store, pool = make_store(factory, database, monkeypatch)
    values = {"available_int": 0} if factory is DocumentInfinityConnection else {"status": 0}

    with pytest.raises(InfinityException) as caught:
        store.update({"id": "row"}, values, "index", "dataset")

    assert caught.value is failure
    assert pool.release_count == 1


@pytest.mark.parametrize("factory", [DocumentInfinityConnection, MemoryInfinityConnection])
def test_successful_update_returns_true(factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch) -> None:
    table = FakeTable()
    store, pool = make_store(factory, FakeDatabase(table), monkeypatch)
    values = {"available_int": 0} if factory is DocumentInfinityConnection else {"status": 0}

    assert store.update({"id": "row"}, values, "index", "dataset") is True
    assert len(table.updates) == 1
    assert pool.release_count == 1


class StatusSession(Session):
    """Explicit unit SQL boundary; production SQL is tested in integration."""

    def __init__(self, doc: Document) -> None:
        self.doc = doc
        self.pending: str | None = None

    def scalar(self, statement: Any) -> Document:
        return self.doc

    def execute(self, statement: Any) -> SimpleNamespace:
        self.pending = statement.compile().params["status"]
        return SimpleNamespace(rowcount=1)

    def commit(self) -> None:
        if self.pending is not None:
            self.doc.status = self.pending
        self.pending = None

    def rollback(self) -> None:
        self.pending = None


@pytest.mark.parametrize("failure", [False, RuntimeError("3022 in an unrelated provider error")])
def test_rest_and_legacy_status_share_safe_recoverable_store_failure(failure: bool | Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.services import document_status_service

    doc = Document(id="doc", kb_id="kb", status="1", chunk_num=2)
    kb = Knowledgebase(id="kb", tenant_id="tenant", name="name")
    writes: list[int] = []

    def update(condition: Any, values: dict[str, Any], *args: Any) -> bool:
        writes.append(values["available_int"])
        if len(writes) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return True

    monkeypatch.setattr(document_api_service.settings, "docStoreConn", SimpleNamespace(update=update), raising=False)
    db = StatusSession(doc)
    response = document_api_service.update_document_status_only(db, 0, doc, kb)
    assert response is not None
    assert json.loads(response.body)["code"] == RetCode.SERVER_ERROR
    assert json.loads(response.body)["message"] == STATUS_ERROR
    assert doc.status == "1" and writes == [0, 1]

    async def shared(*args: Any, **kwargs: Any) -> dict[str, dict[str, str]]:
        writes.clear()
        error = document_status_service.change_document_status_sync(db, doc, kb, "0")
        return {"doc": {"error": error}}

    monkeypatch.setattr(document_app, "batch_document_status", shared)
    import asyncio

    response = asyncio.run(
        document_app.change_status(
            document_app.ChangeStatusRequest(doc_ids=["doc"], status=0),
            db=AsyncSession(),
            user=build_principal_from_authenticated_actor(
                actor=AuthenticatedActor(platform_user_id="owner"),
                membership=TenantMembershipEvidence(platform_user_id="owner", tenant_id="owner"),
                authentication=AuthenticationContext(source=AuthenticationSource.WEB_SESSION, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
            ),
        )
    )
    assert json.loads(response.body) == {"code": 500, "message": "Partial failure", "data": {"doc": {"error": STATUS_ERROR}}}
    assert doc.status == "1" and writes == [0, 1]


def test_rest_document_status_skips_store_for_unparsed_document(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = Document(id="doc", kb_id="kb", status="1", chunk_num=0)
    kb = Knowledgebase(id="kb", tenant_id="tenant", name="name")

    def unexpected_update(*args: Any, **kwargs: Any) -> bool:
        pytest.fail("unparsed document with no table should not update the store")

    monkeypatch.setattr(document_api_service.settings, "docStoreConn", SimpleNamespace(update=unexpected_update, index_exist=lambda *args: False), raising=False)
    assert document_api_service.update_document_status_only(StatusSession(doc), 0, doc, kb) is None
    assert doc.status == "0"
