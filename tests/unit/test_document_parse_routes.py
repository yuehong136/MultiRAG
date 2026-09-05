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
from api.db.services.task_service import TaskService
from common import settings


def _doc(doc_id="d1", run="0", kb_id="kb1"):
    obj = types.SimpleNamespace(id=doc_id, run=run, kb_id=kb_id)
    obj.to_dict = lambda: {"id": doc_id, "kb_id": kb_id, "run": run}
    return obj


@pytest.fixture
def parse_stubs(monkeypatch, fake_kb):
    """把解析链上的写侧全部换成记录器，只留被测编排逻辑。"""
    recorded: dict[str, list] = {"updates": [], "runs": [], "cleared": [], "task_deletes": [], "store_deletes": []}

    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kb_id: fake_kb(id=kb_id, tenant_id="t1", name="ds")))
    monkeypatch.setattr(DocumentService, "update_by_id", classmethod(lambda cls, s, doc_id, info: recorded["updates"].append((doc_id, info)) or True))
    monkeypatch.setattr(DocumentService, "clear_chunk_num_when_rerun", classmethod(lambda cls, s, doc_id: recorded["cleared"].append(doc_id)))
    monkeypatch.setattr(DocumentService, "run", classmethod(lambda cls, s, tid, doc, m: recorded["runs"].append(doc["id"])))
    monkeypatch.setattr(TaskService, "filter_delete", classmethod(lambda cls, s, filters: recorded["task_deletes"].append(filters) or 0))
    monkeypatch.setattr(
        settings,
        "docStoreConn",
        types.SimpleNamespace(
            db_type=lambda: "elasticsearch",
            index_exist=lambda idx, kb_id: True,
            delete=lambda cond, idx, kb_id: recorded["store_deletes"].append((cond, idx)),
        ),
    )
    return recorded


def test_parse_requeues_documents_and_clears_the_index(db, parse_stubs, monkeypatch):
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: [_doc()]))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda cls, s, doc_id: _doc(doc_id)))

    result = document_api_service.parse_dataset_documents(db, "kb1", "t1", ["d1"], [])

    assert result == {"success_count": 1}
    assert parse_stubs["updates"] == [("d1", {"run": "1", "progress": 0})]
    assert parse_stubs["runs"] == ["d1"]
    assert parse_stubs["store_deletes"] == [({"doc_id": "d1"}, "multirag_t1_ds")]
    # 未完成的文档不该被清 chunk 计数
    assert parse_stubs["cleared"] == []


def test_parse_resets_counters_when_rerunning_a_finished_document(db, parse_stubs, monkeypatch):
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: [_doc(run="3")]))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda cls, s, doc_id: _doc(doc_id, run="3")))

    document_api_service.parse_dataset_documents(db, "kb1", "t1", ["d1"], [])

    assert parse_stubs["cleared"] == ["d1"]
    assert parse_stubs["updates"] == [("d1", {"run": "1", "progress": 0, "progress_msg": "", "chunk_num": 0, "token_num": 0})]


def test_parse_uses_milvus_collection_probe(db, parse_stubs, monkeypatch):
    """milvus 没有 index_exist；错走那条分支会 AttributeError 而不是静默跳过。"""
    probed: list[str] = []
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: [_doc()]))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda cls, s, doc_id: _doc(doc_id)))
    monkeypatch.setattr(
        settings,
        "docStoreConn",
        types.SimpleNamespace(
            db_type=lambda: "milvus",
            has_collection=lambda name: probed.append(name) or True,
            delete=lambda condition, index_name, dataset_id: parse_stubs["store_deletes"].append((condition, index_name)),
        ),
    )

    document_api_service.parse_dataset_documents(db, "kb1", "t1", ["d1"], [])

    assert probed == ["multirag_t1_ds"]
    assert parse_stubs["store_deletes"] == [({"doc_id": "d1"}, "multirag_t1_ds")]


def test_parse_rejects_documents_outside_the_dataset(db, monkeypatch):
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: []))

    with pytest.raises(document_api_service.DocumentParseError, match=r"Documents not found: \['ghost'\]"):
        document_api_service.parse_dataset_documents(db, "kb1", "t1", ["ghost"], [])


def test_parse_continues_with_valid_documents_and_reports_the_rest(db, parse_stubs, monkeypatch):
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: [] if kw["id"] == "ghost" else [_doc(kw["id"])]))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda cls, s, doc_id: _doc(doc_id)))

    with pytest.raises(document_api_service.DocumentParseError) as exc:
        document_api_service.parse_dataset_documents(db, "kb1", "t1", ["d1", "ghost"], [])
    assert exc.value.result == {"success_count": 1, "errors": ["Documents not found: ['ghost']"]}
    assert parse_stubs["runs"] == ["d1"]


def test_stop_cancels_running_documents(db, monkeypatch):
    cancelled: list[str] = []
    updates: list[tuple] = []

    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: [_doc(run="1")]))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda cls, s, doc_id: _doc(doc_id, run="1")))
    monkeypatch.setattr(DocumentService, "update_by_id", classmethod(lambda cls, s, doc_id, info: updates.append((doc_id, info)) or True))
    monkeypatch.setattr(TaskService, "query", classmethod(lambda cls, s, **kw: []))
    monkeypatch.setattr(document_api_service, "cancel_all_task_of", lambda s, doc_id: cancelled.append(doc_id))

    result = document_api_service.stop_dataset_documents(db, "kb1", ["d1"], [])

    assert result == {"success_count": 1}
    assert cancelled == ["d1"]
    assert updates == [("d1", {"run": "2"})]


def test_stop_refuses_documents_that_never_started(db, monkeypatch):
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: [_doc(run="0")]))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda cls, s, doc_id: _doc(doc_id, run="0")))
    monkeypatch.setattr(TaskService, "query", classmethod(lambda cls, s, **kw: []))

    result = document_api_service.stop_dataset_documents(db, "kb1", ["d1"], [])

    assert result == {"success_count": 0, "errors": ["Can't stop parsing document that has not started or already completed"]}


def test_stop_allows_a_done_document_with_an_unfinished_task(db, monkeypatch):
    """run 已是 DONE 但仍有未完成 task 时必须可停——否则卡住的任务无法取消。"""
    cancelled: list[str] = []

    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: [_doc(run="3")]))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda cls, s, doc_id: _doc(doc_id, run="3")))
    monkeypatch.setattr(TaskService, "query", classmethod(lambda cls, s, **kw: [types.SimpleNamespace(progress=0.5)]))
    monkeypatch.setattr(DocumentService, "update_by_id", classmethod(lambda cls, s, doc_id, info: True))
    monkeypatch.setattr(document_api_service, "cancel_all_task_of", lambda s, doc_id: cancelled.append(doc_id))

    assert document_api_service.stop_dataset_documents(db, "kb1", ["d1"], []) == {"success_count": 1}
    assert cancelled == ["d1"]


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

    resp = client.post("/api/v1/datasets/kb1/documents/parse", json={"document_ids": ["d1"]})

    assert resp.json()["message"] == "You don't own the dataset kb1."


def test_parse_route_requires_document_ids(client):
    assert client.post("/api/v1/datasets/kb1/documents/parse", json={"document_ids": []}).status_code == 422


def test_stop_route_shape(client, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, user_id: True))
    monkeypatch.setattr(document_api_service, "stop_dataset_documents", lambda s, d, ids, errs: {"success_count": len(ids)})

    resp = client.post("/api/v1/datasets/kb1/documents/stop", json={"document_ids": ["d1"]})

    assert resp.status_code == 200
    assert resp.json()["data"] == {"success_count": 1}


def test_stop_route_surfaces_missing_documents(client, monkeypatch):
    def _raise(s, d, ids, errs):
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

    def blocking_workflow(session, *args):
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
