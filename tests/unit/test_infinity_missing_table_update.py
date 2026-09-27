import json
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from infinity.common import InfinityException
from infinity.errors import ErrorCode
from sqlalchemy.orm import Session

from api.apps import document_app
from api.apps.services import document_api_service
from api.db.db_models import Document, Knowledgebase
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


@pytest.mark.parametrize(
    ("update_result", "expected_error"),
    [
        (False, "Document store table missing or update failed."),
        (RuntimeError("3022 in an unrelated error"), "Document store update failed."),
    ],
)
def test_legacy_document_status_reports_store_failure(
    update_result: bool | Exception,
    expected_error: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = SimpleNamespace(kb_id="kb", status="1", chunk_num=2)
    kb = SimpleNamespace(tenant_id="tenant", name="name")
    monkeypatch.setattr(document_app.DocumentService, "accessible", lambda *args, **kwargs: True)
    monkeypatch.setattr(document_app.DocumentService, "get_by_id", lambda *args, **kwargs: doc)
    monkeypatch.setattr(document_app.DocumentService, "update_by_id", lambda *args, **kwargs: True)
    monkeypatch.setattr(document_app.KnowledgebaseService, "get_by_id", lambda *args, **kwargs: kb)
    monkeypatch.setattr(document_app.search, "index_name_one", lambda *args, **kwargs: "index")

    def update(*args: Any, **kwargs: Any) -> bool:
        if isinstance(update_result, Exception):
            raise update_result
        return update_result

    monkeypatch.setattr(document_app.settings, "docStoreConn", SimpleNamespace(update=update), raising=False)
    with Session() as db:
        response = document_app.change_status(document_app.ChangeStatusRequest(doc_ids=["doc"], status=0), db=db, user=SimpleNamespace(id="owner"))

    payload = json.loads(response.body)
    assert payload["code"] == RetCode.SERVER_ERROR
    assert payload["data"]["doc"] == {"error": expected_error}


def test_rest_document_status_rejects_false_store_update(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = Document(id="doc", kb_id="kb", status="1", chunk_num=2)
    kb = Knowledgebase(id="kb", tenant_id="tenant", name="name")
    monkeypatch.setattr(document_api_service.DocumentService, "update_by_id", lambda *args, **kwargs: True)
    monkeypatch.setattr(document_api_service.search, "index_name", lambda *args, **kwargs: "index")
    monkeypatch.setattr(document_api_service.settings, "docStoreConn", SimpleNamespace(update=lambda *args, **kwargs: False), raising=False)

    with Session() as db:
        response = document_api_service.update_document_status_only(db, 0, doc, kb)

    assert response is not None
    payload = json.loads(response.body)
    assert payload["code"] == RetCode.DATA_ERROR
    assert payload["message"] == "Document store table missing or update failed."


def test_rest_document_status_skips_store_for_unparsed_document(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = Document(id="doc", kb_id="kb", status="1", chunk_num=0)
    kb = Knowledgebase(id="kb", tenant_id="tenant", name="name")
    monkeypatch.setattr(document_api_service.DocumentService, "update_by_id", lambda *args, **kwargs: True)

    def unexpected_update(*args: Any, **kwargs: Any) -> bool:
        pytest.fail("unparsed document should not update the document store")

    monkeypatch.setattr(document_api_service.settings, "docStoreConn", SimpleNamespace(update=unexpected_update), raising=False)

    with Session() as db:
        response = document_api_service.update_document_status_only(db, 0, doc, kb)

    assert response is None
