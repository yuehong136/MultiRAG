"""
Dataset RESTful service 层单元测试 —— delete_datasets
对齐 ragflow test_dataset_mangement/test_delete_datasets.py 场景。
共享 fixtures（db / fake_kb）见 tests/unit/conftest.py。
"""

import types

import api.apps.services.dataset_api_service as svc


class FakeDocStore:
    def __init__(self, db_type="milvus"):
        self._db_type = db_type
        self.calls = []

    def db_type(self):
        return self._db_type

    def delete(self, cond, idx, kb_id):
        self.calls.append(("delete", cond, idx, kb_id))

    def delete_idx(self, idx, kb_id):
        self.calls.append(("delete_idx", idx, kb_id))


def _patch_search(monkeypatch):
    fake_search = types.SimpleNamespace(
        index_name=lambda tid: ["idx_tenant"],
        index_name_one=lambda tid, name: "idx_one",
    )
    monkeypatch.setattr(svc, "search", fake_search)


def _patch_delete_deps(monkeypatch, fake_kb, store, storage_impl, delete_ok=True):
    monkeypatch.setattr(svc.settings, "docStoreConn", store)
    monkeypatch.setattr(svc.settings, "STORAGE_IMPL", storage_impl)
    monkeypatch.setattr(svc.KnowledgebaseService, "get_or_none", lambda d, **kw: fake_kb(id=kw.get("id")))
    monkeypatch.setattr(svc.DocumentService, "query", lambda d, **kw: [])
    monkeypatch.setattr(svc.FileService, "filter_delete", lambda d, flt: 0)
    if callable(delete_ok):
        monkeypatch.setattr(svc.KnowledgebaseService, "delete_by_id", lambda d, kb_id: delete_ok(kb_id))
    else:
        monkeypatch.setattr(svc.KnowledgebaseService, "delete_by_id", lambda d, kb_id: delete_ok)


def test_delete_noop_when_no_ids(db):
    ok, data = svc.delete_datasets(db, "t1", ids=None, delete_all=False)
    assert ok is True
    assert data is None


def test_delete_unknown_id_returns_permission_error(monkeypatch, db):
    monkeypatch.setattr(svc.KnowledgebaseService, "get_or_none", lambda d, **kw: None)
    ok, msg = svc.delete_datasets(db, "t1", ids=["nope"])
    assert ok is False
    assert "lacks permission" in msg


def test_delete_success_milvus(monkeypatch, db, fake_kb):
    store = FakeDocStore(db_type="milvus")
    _patch_search(monkeypatch)
    _patch_delete_deps(monkeypatch, fake_kb, store, types.SimpleNamespace())  # 无 remove_bucket

    ok, data = svc.delete_datasets(db, "t1", ids=["kb1"])
    assert ok is True
    assert data is None
    # milvus 走 index_name_one + delete_idx，不应有按 kb_id 的 delete(content)
    assert ("delete_idx", "idx_one", "kb1") in store.calls
    assert not any(c[0] == "delete" for c in store.calls)


def test_delete_es_deletes_content_before_drop(monkeypatch, db, fake_kb):
    """ES/OpenSearch 共享索引：先 delete({kb_id}) 再 delete_idx（承接旧 web）。"""
    store = FakeDocStore(db_type="elasticsearch")
    _patch_search(monkeypatch)
    _patch_delete_deps(monkeypatch, fake_kb, store, types.SimpleNamespace())

    ok, _ = svc.delete_datasets(db, "t1", ids=["kb1"])
    assert ok is True
    ops = [c[0] for c in store.calls]
    assert ops == ["delete", "delete_idx"]  # 顺序：先删内容再 drop


def test_delete_calls_remove_bucket_when_supported(monkeypatch, db, fake_kb):
    store = FakeDocStore(db_type="milvus")
    _patch_search(monkeypatch)
    removed = []
    storage = types.SimpleNamespace(remove_bucket=lambda kb_id: removed.append(kb_id))
    _patch_delete_deps(monkeypatch, fake_kb, store, storage)

    ok, _ = svc.delete_datasets(db, "t1", ids=["kb1"])
    assert ok is True
    assert removed == ["kb1"]


def test_delete_partial_failure(monkeypatch, db, fake_kb):
    store = FakeDocStore(db_type="milvus")
    _patch_search(monkeypatch)
    # kb1 删除成功，kb2 失败
    _patch_delete_deps(monkeypatch, fake_kb, store, types.SimpleNamespace(), delete_ok=lambda kb_id: kb_id == "kb1")

    ok, data = svc.delete_datasets(db, "t1", ids=["kb1", "kb2"])
    assert ok is True
    assert data["success_count"] == 1
    assert data["errors"]
