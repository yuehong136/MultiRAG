"""RESTful 文档解析/停止端点的服务与路由契约。

这两条端点取代了 legacy `/document/run` 的 run=1 / run=2 多态分支，
所以 doc-store 清理必须保留 legacy 分支里的 milvus 差异：milvus 没有
index_exist，走 has_collection + 每数据集一个 collection。
"""

import types
from contextlib import contextmanager

import pytest
from sqlalchemy.orm import Session

from api.apps.services import document_api_service
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService


def _doc(doc_id="d1", run="0", kb_id="kb1"):
    obj = types.SimpleNamespace(id=doc_id, run=run, kb_id=kb_id)
    obj.to_dict = lambda: {"id": doc_id, "kb_id": kb_id, "run": run}
    return obj


@pytest.fixture
def parse_stubs(monkeypatch):
    """The public endpoints pass explicit policy to the reliable ingestion unit."""
    calls = []

    def ingest(session, ids, principal, run, clear=False, apply_kb=False, dataset_id=None):
        calls.append((ids, principal, run, clear, apply_kb, dataset_id))
        return True

    monkeypatch.setattr(document_api_service, "ingest_documents_sync", ingest)
    return calls


def test_parse_requeues_documents_and_clears_the_index(db, parse_stubs, monkeypatch):
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: [_doc()]))
    assert document_api_service.parse_dataset_documents(db, "kb1", "t1", ["d1"], []) == {"success_count": 1}
    assert parse_stubs == [(["d1"], "t1", "1", True, False, "kb1")]


def test_parse_resets_counters_when_rerunning_a_finished_document(db, parse_stubs, monkeypatch):
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: [_doc(run="3")]))
    assert document_api_service.parse_dataset_documents(db, "kb1", "t1", ["d1"], []) == {"success_count": 1}
    # Clear-first applies to completed and partial ledgers alike; storage and
    # atomic Doc/KB counters are covered by the real ingestion regression.
    assert parse_stubs[0][2:4] == ("1", True)


def test_parse_uses_milvus_collection_probe(monkeypatch):
    from common.doc_store.document_history import document_history

    probed = []
    connection = types.SimpleNamespace(has_collection=lambda name: probed.append(name) or False)
    store = types.SimpleNamespace(db_type=lambda: "milvus", _get_connection=lambda: connection)
    assert document_history(store, "multirag_t1_ds", "kb1", "d1") == []
    assert probed == ["multirag_t1_ds"]


def test_parse_rejects_documents_outside_the_dataset(db, monkeypatch):
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: []))
    with pytest.raises(document_api_service.DocumentParseError, match=r"Documents not found: \['ghost'\]"):
        document_api_service.parse_dataset_documents(db, "kb1", "t1", ["ghost"], [])


def test_parse_continues_with_valid_documents_and_reports_the_rest(db, parse_stubs, monkeypatch):
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: [] if kw["id"] == "ghost" else [_doc(kw["id"])]))
    with pytest.raises(document_api_service.DocumentParseError) as exc:
        document_api_service.parse_dataset_documents(db, "kb1", "t1", ["d1", "ghost"], [])
    assert exc.value.result == {"success_count": 1, "errors": ["Documents not found: ['ghost']"]}
    assert parse_stubs[0][0] == ["d1"]


def test_stop_cancels_running_documents(db, parse_stubs, monkeypatch):
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: [_doc(run="1")]))
    assert document_api_service.stop_dataset_documents(db, "kb1", ["d1"], [], principal_id="t1") == {"success_count": 1}
    assert parse_stubs == [(["d1"], "t1", "2", False, False, "kb1")]


def test_stop_refuses_documents_that_never_started(db, monkeypatch):
    from api.db.services.document_ingest_service import IngestError

    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: [_doc(run="0")]))

    def terminal(*args, **kwargs):
        raise IngestError("Document has no active parsing task to cancel.", result={"results": {"d1": {"error": "Document has no active parsing task to cancel."}}})

    monkeypatch.setattr(document_api_service, "ingest_documents_sync", terminal)
    with pytest.raises(document_api_service.DocumentParseError) as exc:
        document_api_service.stop_dataset_documents(db, "kb1", ["d1"], [], principal_id="t1")
    assert exc.value.result["success_count"] == 0
    assert "no active" in exc.value.result["results"]["d1"]["error"]


def test_stop_allows_a_done_document_with_an_unfinished_task(db, parse_stubs, monkeypatch):
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: [_doc(run="3")]))
    assert document_api_service.stop_dataset_documents(db, "kb1", ["d1"], [], principal_id="t1") == {"success_count": 1}
    assert parse_stubs[0][2:4] == ("2", False)


# ---------------------------------------------------------------------------
# 路由层
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def worker_session(monkeypatch):
    @contextmanager
    def connection():
        with Session() as session:
            yield session

    monkeypatch.setattr(document_api_service, "db_connection", connection)


def test_parse_route_shape(client, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, user_id: True))
    monkeypatch.setattr(document_api_service, "parse_dataset_documents", lambda s, d, t, ids, errs: {"success_count": len(ids)})

    resp = client.post("/api/v1/datasets/kb1/documents/parse", json={"document_ids": ["d1", "d2"]})

    assert resp.status_code == 200
    assert resp.json()["data"] == {"success_count": 2}


def test_parse_route_denies_foreign_dataset(client, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, user_id: False))

    def denied(*args):
        raise document_api_service.DocumentParseError("Document selection unavailable or not writable.")

    monkeypatch.setattr(document_api_service, "parse_dataset_documents", denied)
    resp = client.post("/api/v1/datasets/kb1/documents/parse", json={"document_ids": ["d1"]})
    assert resp.json()["code"] != 0
    assert resp.json()["message"] == "Document selection unavailable or not writable."


def test_parse_route_requires_document_ids(client):
    assert client.post("/api/v1/datasets/kb1/documents/parse", json={"document_ids": []}).status_code == 422


def test_stop_route_shape(client, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, user_id: True))
    monkeypatch.setattr(document_api_service, "stop_dataset_documents", lambda s, d, ids, errs, **kwargs: {"success_count": len(ids)})

    resp = client.post("/api/v1/datasets/kb1/documents/stop", json={"document_ids": ["d1"]})

    assert resp.status_code == 200
    assert resp.json()["data"] == {"success_count": 1}


def test_stop_route_surfaces_missing_documents(client, monkeypatch):
    def _raise(s, d, ids, errs, **kwargs):
        raise document_api_service.DocumentParseError("Documents not found: ['ghost']")

    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, user_id: True))
    monkeypatch.setattr(document_api_service, "stop_dataset_documents", _raise)

    resp = client.post("/api/v1/datasets/kb1/documents/stop", json={"document_ids": ["ghost"]})

    assert resp.json()["message"] == "Documents not found: ['ghost']"


def test_parse_route_dedupes_document_ids(client, monkeypatch):
    seen: list[list[str]] = []
    reported: list[list[str]] = []

    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, user_id: True))
    monkeypatch.setattr(
        document_api_service,
        "parse_dataset_documents",
        lambda s, d, t, ids, errs: ((seen.append(ids), reported.append(errs)) and None) or {"success_count": len(ids)},
    )

    client.post("/api/v1/datasets/kb1/documents/parse", json={"document_ids": ["d1", "d1", "d2"]})

    # check_duplicate_ids 去重但不保序（list(set(...))），只锁集合与重复告警
    assert sorted(seen[0]) == ["d1", "d2"]
    assert reported == [["Duplicate document ids: d1"]]


@pytest.mark.parametrize("operation", ["parse", "stop"])
async def test_document_workflow_keeps_blocking_io_and_session_on_worker(monkeypatch, worker_session, operation):
    import asyncio
    import threading

    loop_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    worker_threads = []
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, db, kb, user: worker_threads.append(threading.get_ident()) or True))

    def blocking_workflow(session, *args, **kwargs):
        assert isinstance(session, Session)
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return {"success_count": 1}

    monkeypatch.setattr(document_api_service, f"{operation}_dataset_documents", blocking_workflow)
    task = asyncio.create_task(getattr(document_api_service, f"{operation}_dataset_documents_async")("kb1", "t1", ["d1"], []))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not task.done()
        assert len(set(worker_threads)) == 1 and worker_threads[0] != loop_thread
    finally:
        release.set()
    assert await task == {"success_count": 1}


def test_parse_partial_failure_returns_error_code_and_completed_count(client, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb, user: True))

    def partial(*args):
        raise document_api_service.DocumentParseError("Documents not found: ['missing']", {"success_count": 1, "errors": ["missing"]})

    monkeypatch.setattr(document_api_service, "parse_dataset_documents", partial)
    body = client.post("/api/v1/datasets/kb1/documents/parse", json={"document_ids": ["d1", "missing"]}).json()
    assert body["code"] != 0 and body["data"]["success_count"] == 1 and "missing" in body["message"]
