"""Ingest input, wire and complete per-document submission outcomes."""

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from api.db.services import document_ingest_service as service
from api.utils.validation_utils import DocumentIngestRequest
from common.constants import RetCode


@pytest.mark.parametrize("run", [0, 1, 2, "0", "1", "2"])
def test_exact_operations_and_first_seen_deduplication(run: int | str) -> None:
    request = DocumentIngestRequest.model_validate({"doc_ids": ["second", "first", "second"], "run": run})
    assert request.run == str(run) and request.doc_ids == ["second", "first"]
    assert request.delete is False and request.apply_kb is False


@pytest.mark.parametrize("run", [True, False, 0.0, 1.0, 2.0, 3, -1, "01", " 1", "", None, [], {}])
def test_rejects_coerced_or_unknown_operation(run: Any) -> None:
    with pytest.raises(ValidationError):
        DocumentIngestRequest.model_validate({"doc_ids": ["doc"], "run": run})


@pytest.mark.parametrize("ids", [[], "doc", None, [1], [True], [{}], [None], [""], [" "], ("doc",)])
def test_requires_nonempty_json_array_of_real_ids(ids: Any) -> None:
    with pytest.raises(ValidationError):
        DocumentIngestRequest.model_validate({"doc_ids": ids, "run": 1})


@pytest.mark.parametrize("field", ["delete", "apply_kb"])
@pytest.mark.parametrize("value", ["true", "false", 0, 1, None, [], {}])
def test_options_are_real_booleans(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        DocumentIngestRequest.model_validate({"doc_ids": ["doc"], "run": 1, field: value})


@pytest.mark.parametrize(
    "payload",
    [{"doc_ids": ["doc"]}, {"run": 1}, {"doc_ids": ["doc"], "run": 1, "tenant_id": "forged"}, {"doc_ids": ["doc"], "run": 0, "apply_kb": True}, {"doc_ids": ["doc"], "run": 2, "apply_kb": True}],
)
def test_missing_extra_and_inapplicable_metadata_options(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DocumentIngestRequest.model_validate(payload)


@pytest.mark.parametrize("bad_effect", [None, False, {}, {"run": "2"}])
def test_missing_effect_is_nonzero_with_every_requested_id(monkeypatch: pytest.MonkeyPatch, bad_effect: Any) -> None:
    selections = [service.Selection(name, "kb", frozenset(), 1) for name in ["submitted", "failed", "later"]]
    monkeypatch.setattr(service, "_operate", lambda selected, *args: bad_effect if selected.document_id == "failed" else {"run": "1"})
    with pytest.raises(service.IngestError) as failure:
        service.ingest_selected(selections, "owner", "1", False, False)
    assert failure.value.code == RetCode.SERVER_ERROR
    assert failure.value.result == {"results": {"submitted": {"run": "1"}, "failed": {"error": "Document ingestion effect could not be confirmed."}, "later": {"run": "1"}}}


def test_ingest_wire_preserves_nonzero_data(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    results = {"first": {"run": "1"}, "second": {"error": "Document ingestion failed; retry to reconcile."}}

    async def partial(*args: Any) -> bool:
        raise service.IngestError("Document ingestion was not fully submitted.", RetCode.SERVER_ERROR, {"results": results})

    monkeypatch.setitem(vars(sys.modules["api.apps.restful_apis.document"]), "ingest_documents", partial)
    response = client.post("/api/v1/documents/ingest", json={"doc_ids": ["first", "second"], "run": 1})
    assert response.status_code == 200
    assert response.json() == {"code": 500, "message": "Document ingestion was not fully submitted.", "data": {"results": results}}


@pytest.mark.parametrize("effect", [False, None, {"results": {}}])
def test_ingest_wire_does_not_acknowledge_unconfirmed_success(client: Any, monkeypatch: pytest.MonkeyPatch, effect: Any) -> None:
    async def missing(*args: Any) -> Any:
        return effect

    monkeypatch.setitem(vars(sys.modules["api.apps.restful_apis.document"]), "ingest_documents", missing)
    response = client.post("/api/v1/documents/ingest", json={"doc_ids": ["first"], "run": 1})
    assert response.status_code == 200 and response.json()["code"] == 500 and response.json()["data"] is None


@pytest.mark.parametrize("effect", [False, None, 1, "true", {"ok": True}])
def test_admin_requires_formal_complete_acknowledgement(monkeypatch: pytest.MonkeyPatch, effect: Any, capsys: pytest.CaptureFixture[str]) -> None:
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "admin/client"))
    from admin.client.multirag_client import MultiRAGClient

    http = SimpleNamespace(request=lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: {"code": 0, "data": effect}))
    client = MultiRAGClient(http, "user")
    assert client._submit_document_ingestion(["doc"]) is False
    assert "Parsing submitted" not in capsys.readouterr().out


@pytest.mark.parametrize("result", [{"code": 500, "data": True}, {"code": False, "data": True}, [True], None, {"retcode": 0, "data": True}])
def test_admin_rejects_wrong_wire_or_error_business_code(result: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "admin/client"))
    from admin.client.multirag_client import MultiRAGClient

    client = MultiRAGClient(SimpleNamespace(request=lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: result)), "user")
    assert client._submit_document_ingestion(["doc"]) is False


def test_legacy_pipeline_run_rejects_false_queue_ack(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.services import task_service
    from api.db.services.document_service import DocumentService

    monkeypatch.setattr(task_service, "queue_dataflow", lambda *args, **kwargs: (False, "failure"))
    with pytest.raises(ConnectionError, match="queue document pipeline"):
        DocumentService.run(db, "owner", {"id": "doc", "pipeline_id": "pipeline"}, {})


def test_token_ledger_is_not_generated_progress_text() -> None:
    from api.db.services.document_task_service import accounted_tokens, base_task_digest, token_digest
    from api.db.services.task_service import reuse_prev_task_chunks

    digest = token_digest("hash", 7)
    previous = [{"from_page": 0, "digest": digest, "progress": 1, "chunk_ids": "child", "progress_msg": "untrusted parser text"}]
    task = {"from_page": 0, "to_page": 1, "digest": "hash"}
    assert reuse_prev_task_chunks(task, previous, {}) == 1
    assert base_task_digest(digest) == "hash" and accounted_tokens(digest) == 7
    assert accounted_tokens("hash:ingest-tokens:not-an-integer") is None
    assert task["chunk_ids"] == "child" and task["progress"] == 1
    assert accounted_tokens(task["digest"]) == 7


@pytest.mark.asyncio
async def test_expired_jwt_does_not_query_api_key_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from fastapi import HTTPException
    from sqlalchemy.ext.asyncio import AsyncSession
    from starlette.requests import Request

    from api.apps import manager
    from api.db.services.api_service import APITokenService
    from api.utils.api_utils import async_current_user

    token = manager.create_access_token(data={"sub": "expired@test.local"}, expires=timedelta(seconds=-1))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Expired JWT attempted API-key identity")

    monkeypatch.setattr(APITokenService, "query", forbidden)
    with pytest.raises(HTTPException):
        await async_current_user(Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]}), AsyncSession())


def test_durable_material_roundtrip_preserves_native_values_and_source_dicts() -> None:
    from datetime import UTC, date, datetime
    from decimal import Decimal

    import numpy as np

    from api.db.services.document_ingest_recovery import _read_wire, _wire

    snapshot = {
        "rows": [
            {
                "vector": np.array([0.25, 0.5], dtype=np.float32),
                "decimal": Decimal("1.234567890123456789"),
                "date": date(2026, 10, 3),
                "created": datetime(2026, 10, 3, tzinfo=UTC),
                "blob": b"\x00\xff",
                "metadata": {"__type__": "datetime", "value": "ordinary source content"},
            }
        ],
        "flags": [("task-cancel", "nonce", b"previous", 1000)],
    }
    restored = _read_wire(_wire(snapshot))
    row = restored["rows"][0]
    assert row["vector"] == [0.25, 0.5]
    assert row["decimal"] == Decimal("1.234567890123456789")
    assert row["date"] == date(2026, 10, 3) and row["created"] == datetime(2026, 10, 3, tzinfo=UTC)
    assert row["blob"] == b"\x00\xff" and row["metadata"] == snapshot["rows"][0]["metadata"]
    assert restored["flags"] == [["task-cancel", "nonce", b"previous", 1000]]
    with pytest.raises(TypeError, match="Unknown ingestion recovery value"):
        _wire({"unsupported": object()})


@pytest.mark.parametrize("ack", [True, False, None, "1", 0])
def test_recovery_compare_and_swap_failure_keeps_original_material(monkeypatch: pytest.MonkeyPatch, ack: Any) -> None:
    from types import SimpleNamespace

    from api.db.services import document_ingest_recovery as recovery

    saved = recovery.StoredRecovery.prepare("doc", snapshot=[{"id": "original"}], nonce="owned")
    original_wire = saved.wire
    redis = SimpleNamespace(eval=lambda *args: ack, get=lambda *args: b"later-owner")
    monkeypatch.setattr(recovery.REDIS_CONN, "REDIS", redis)
    with pytest.raises(RuntimeError, match="ownership changed"):
        saved.update(phase="failed")
    assert saved.wire == original_wire and saved.data["phase"] == "active"
    with pytest.raises(RuntimeError, match="ownership changed"):
        saved.clear()


@pytest.mark.parametrize("accepted", [False, True])
def test_recovery_material_lost_set_response_requires_exact_readback(monkeypatch: pytest.MonkeyPatch, accepted: bool) -> None:
    from types import SimpleNamespace

    from api.db.services import document_ingest_recovery as recovery

    saved = recovery.StoredRecovery.prepare("doc", snapshot=[{"id": "original"}], nonce="owned")

    def lost_response(*args: Any, **kwargs: Any) -> None:
        assert kwargs == {"nx": True}
        raise ConnectionError("controlled lost response")

    monkeypatch.setattr(recovery.REDIS_CONN, "REDIS", SimpleNamespace(set=lost_response, get=lambda *args: saved.wire.encode() if accepted else b"later-owner"))
    if accepted:
        saved.create()
    else:
        with pytest.raises(RuntimeError, match="could not be confirmed"):
            saved.create()


def test_batch_boundary_exception_preserves_all_results_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    selections = [service.Selection(name, "kb", frozenset(), 1) for name in ["submitted", "unknown", "later"]]

    def operate(selected: service.Selection, *args: Any) -> dict[str, Any]:
        if selected.document_id == "unknown":
            raise ConnectionError("private transport detail")
        return {"run": "1"}

    monkeypatch.setattr(service, "_operate", operate)
    with pytest.raises(service.IngestError) as failure:
        service.ingest_selected(selections, "owner", "1", False, False)
    assert failure.value.result is not None
    results = failure.value.result["results"]
    assert set(results) == {"submitted", "unknown", "later"}
    assert results["submitted"] == results["later"] == {"run": "1"}
    assert results["unknown"]["effect"] == "unknown"
    assert "private transport" not in str(results)


@pytest.mark.parametrize(
    "result",
    [
        {"code": 102},
        {"code": False, "data": {"total": 0, "docs": []}},
        {"code": 0},
        {"code": 0, "data": []},
        {"code": 0, "data": {"total": 1, "docs": []}},
        {"code": 0, "data": {"total": 1, "docs": [None]}},
        {"code": 0, "data": {"total": True, "docs": []}},
    ],
)
def test_admin_list_rejects_business_errors_and_malformed_pages(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "admin/client"))
    from admin.client.multirag_client import MultiRAGClient

    client = MultiRAGClient(SimpleNamespace(request=lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: result)), "user")
    assert client._list_documents("dataset", "kb") is None
    assert client._wait_parse_done("dataset", "kb", ["submitted"]) is False
