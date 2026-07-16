"""queue_raptor_o_graphrag_tasks 钉板测试 —— KB 级任务不得触碰真实文档。

回归背景：旧实现先用 sample 文档的真实 id 插 Task 行、begin2parse 也打在真实
文档上，KG/RAPTOR 的任务进度会错误覆盖该文档的运行状态；修复后 DB 行与
begin2parse 一律使用 fake_doc_id（GRAPH_RAPTOR_FAKE_DOC_ID）。
DB / Redis 全部打桩（纯函数打桩形态），共享 fixtures 见 tests/unit/conftest.py。
"""

import api.db.services.document_service as docsvc


def _run(monkeypatch, db, capture):
    monkeypatch.setattr(docsvc.DocumentService, "get_chunking_config", classmethod(lambda cls, d, doc_id: {"parser_id": "naive"}))
    monkeypatch.setattr(docsvc, "bulk_insert_into_db", lambda d, model, rows, replace: capture.setdefault("db_rows", list(rows)))
    monkeypatch.setattr(docsvc.DocumentService, "begin2parse", classmethod(lambda cls, d, doc_id, keep_progress=False: capture.setdefault("begin2parse", (doc_id, keep_progress))))
    monkeypatch.setattr(docsvc.REDIS_CONN, "queue_product", lambda queue, message: capture.setdefault("msg", message) or True)

    sample_doc = {"id": "real-doc-1"}
    return docsvc.queue_raptor_o_graphrag_tasks(db, sample_doc, ty="graphrag", priority=0, fake_doc_id="graph_raptor_x", doc_ids=["real-doc-1", "real-doc-2"])


def test_db_task_row_uses_fake_doc_id(monkeypatch, db):
    cap = {}
    task_id = _run(monkeypatch, db, cap)
    (row,) = cap["db_rows"]
    assert row["doc_id"] == "graph_raptor_x"  # 回归点：旧实现写 real-doc-1
    assert row["id"] == task_id


def test_begin2parse_never_touches_real_document(monkeypatch, db):
    cap = {}
    _run(monkeypatch, db, cap)
    assert cap["begin2parse"] == ("graph_raptor_x", True)


def test_redis_message_keeps_fake_doc_id_and_doc_ids(monkeypatch, db):
    cap = {}
    _run(monkeypatch, db, cap)
    assert cap["msg"]["doc_id"] == "graph_raptor_x"
    assert cap["msg"]["doc_ids"] == ["real-doc-1", "real-doc-2"]
