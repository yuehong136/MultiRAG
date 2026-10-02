"""Real connector/DSL calls with only transport responses controlled."""

import copy
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from common.doc_store.availability import complete_availability_update
from core.utils.es_conn import ESConnection
from core.utils.opensearch_conn import OSConnection


def connector(factory: Callable[..., Any], responses: list[Any]) -> tuple[Any, list[dict[str, Any]]]:
    cls = next(cell.cell_contents for cell in factory.__closure__ if isinstance(cell.cell_contents, type))
    instance = object.__new__(cls)
    instance.logger = logging.getLogger(__name__)
    calls: list[dict[str, Any]] = []

    def transport(**kwargs: Any) -> Any:
        calls.append(kwargs)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    instance.es = instance.os = SimpleNamespace(update_by_query=transport)
    return instance, calls


SUCCESS = {"timed_out": False, "total": 3, "updated": 3, "noops": 0, "version_conflicts": 0, "failures": []}


@pytest.mark.parametrize("factory", [ESConnection, OSConnection])
@pytest.mark.parametrize(
    "changes",
    [{"timed_out": True}, {"version_conflicts": 1}, {"failures": [{"cause": "controlled failure"}]}, {"updated": 2}, {"total": 0, "updated": 0}, {"noops": -1}, {"updated": True}],
)
def test_availability_rejects_incomplete_transport_response(factory: Callable[..., Any], changes: dict[str, Any]) -> None:
    store, calls = connector(factory, [{**SUCCESS, **changes}])
    assert store.update({"doc_id": "doc"}, {"available_int": 1}, "index", "kb") is False
    body = calls[0]["body"]
    assert "mom_id" in body["script"]["source"] and "ctx._id" in body["script"]["source"]
    assert body["script"]["params"] == {"status": 1}
    assert {"term": {"kb_id": "kb"}} in body["query"]["bool"]["filter"]


@pytest.mark.parametrize("factory", [ESConnection, OSConnection])
@pytest.mark.parametrize("updated,noops", [(0, 3), (1, 2), (3, 0)])
def test_availability_accepts_complete_updates_and_noops(factory: Callable[..., Any], updated: int, noops: int) -> None:
    store, _ = connector(factory, [{**SUCCESS, "updated": updated, "noops": noops}])
    assert store.update({"doc_id": "doc"}, {"available_int": 0}, "index", "kb") is True


@pytest.mark.parametrize("response", [None, {}, {"updated": 3}, {**SUCCESS, "total": None}, {**SUCCESS, "failures": None}])
def test_availability_unknown_acknowledgement_fails_closed(response: Any) -> None:
    assert not complete_availability_update(response)


@pytest.mark.parametrize("factory", [ESConnection, OSConnection])
@pytest.mark.parametrize("response", [None, {}, {"updated": 3}, RuntimeError("controlled missing table")])
def test_connector_missing_table_or_unknown_transport_fails_closed(factory: Callable[..., Any], response: Any) -> None:
    store, calls = connector(factory, [response])
    assert store.update({"doc_id": "doc"}, {"available_int": 1}, "index", "kb") is False
    assert len(calls) == 1


@pytest.mark.parametrize("factory", [ESConnection, OSConnection])
def test_availability_failure_recovery_and_same_state_retry(factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.db_models import Document, Knowledgebase
    from api.db.services.document_status_service import STATUS_ERROR, change_document_status_sync
    from common import settings
    from tests.unit.test_infinity_missing_table_update import StatusSession

    store, calls = connector(factory, [{**SUCCESS, "updated": 1}, copy.deepcopy(SUCCESS), {**SUCCESS, "updated": 0, "noops": 3}])
    monkeypatch.setattr(settings, "docStoreConn", store)
    doc = Document(id="doc", kb_id="kb", status="1", chunk_num=3)
    kb = Knowledgebase(id="kb", tenant_id="owner", name="name")
    db = StatusSession(doc)
    assert change_document_status_sync(db, doc, kb, "0") == STATUS_ERROR
    assert doc.status == "1"
    assert [call["body"]["script"]["params"]["status"] for call in calls] == [0, 1]
    assert change_document_status_sync(db, doc, kb, "1") is None
    assert doc.status == "1"
