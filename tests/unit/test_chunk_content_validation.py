"""chunk 写接口空内容校验钉板（web set / sdk add_chunk / sdk update_chunk 三写点）。

回归背景：空或纯空白 content（""、" "、"\\n"）曾绕过校验直达 embedding 模型，
由模型报错兜底且报错语义不明。现约定：三写点统一用
common.string_utils.is_content_empty 在进入分词/向量化前拒绝；
update_chunk 未提供 content 时仍走原有「保留旧内容」分支，不受影响。
路由走真实 ``api.apps.app`` 的 HTTP 契约式，服务层与 doc store 打桩。
"""

from types import SimpleNamespace

from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import token_required
from common import settings
from common.constants import RetCode
from common.string_utils import is_content_empty

# ---- is_content_empty 纯函数 ----


def test_is_content_empty_truth_table():
    assert is_content_empty(None)
    assert is_content_empty("")
    assert is_content_empty(" ")
    assert is_content_empty("\n\t ")
    assert not is_content_empty("text")
    assert not is_content_empty(" x ")


# ---- web POST /v1/chunk/set ----


def test_web_set_rejects_whitespace_content(client):
    resp = client.post("/v1/chunk/set", json={"doc_id": "doc1", "chunk_id": "c1", "content_with_weight": " \n"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == int(RetCode.DATA_ERROR)
    assert body["retmsg"] == "`content_with_weight` is required"


# ---- sdk chunk 端点公共桩 ----

_SDK_CHUNKS = "/api/v1/datasets/kb1/documents/doc1/chunks"


def _stub_sdk_ownership(client, monkeypatch):
    client.app.dependency_overrides[token_required] = lambda: "tenant-unit"
    monkeypatch.setattr(KnowledgebaseService, "query", classmethod(lambda cls, db, **kw: [SimpleNamespace(id="kb1")]))
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, db, **kw: [SimpleNamespace(id="doc1", name="d.txt")]))


# ---- sdk POST .../chunks（add_chunk） ----


def test_sdk_add_chunk_rejects_whitespace_content(client, monkeypatch):
    _stub_sdk_ownership(client, monkeypatch)

    resp = client.post(_SDK_CHUNKS, json={"content": "  \n "})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.DATA_ERROR)
    assert body["message"] == "`content` is required"


def test_sdk_add_chunk_rejects_empty_string_content(client, monkeypatch):
    _stub_sdk_ownership(client, monkeypatch)

    resp = client.post(_SDK_CHUNKS, json={"content": ""})

    body = resp.json()
    assert body["code"] == int(RetCode.DATA_ERROR)
    assert body["message"] == "`content` is required"


# ---- sdk PUT .../chunks/{chunk_id}（update_chunk） ----


class _FakeDocStore:
    def __init__(self, chunk_id, chunk_data):
        self._chunk_id = chunk_id
        self._chunk_data = chunk_data
        self.upserts = []

    def get(self, chunk_id, index_name, kb_id):
        return {self._chunk_id: dict(self._chunk_data)} if chunk_id == self._chunk_id else None

    def upsert(self, chunk_ids, chunk_datas, index_name, kb_id):
        self.upserts.append((chunk_ids, chunk_datas))


def _stub_update_chain(client, monkeypatch):
    _stub_sdk_ownership(client, monkeypatch)
    store = _FakeDocStore("c1", {"content_with_weight": "old content", "available_int": 1})
    monkeypatch.setattr(settings, "docStoreConn", store, raising=False)
    return store


def test_sdk_update_chunk_rejects_whitespace_content(client, monkeypatch):
    store = _stub_update_chain(client, monkeypatch)

    resp = client.put(f"{_SDK_CHUNKS}/c1", json={"content": " "})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.DATA_ERROR)
    assert body["message"] == "`content` is required"
    assert store.upserts == []  # 回归点：空内容绝不落库，也绝不触发 embedding


def test_sdk_update_chunk_without_content_still_updates(client, monkeypatch):
    """未提供 content 时不受空判影响，其余字段照常更新落库。"""
    store = _stub_update_chain(client, monkeypatch)

    resp = client.put(f"{_SDK_CHUNKS}/c1", json={"available": False})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    ((chunk_ids, chunk_datas),) = store.upserts
    assert chunk_ids == ["c1"]
    assert chunk_datas[0]["available_int"] == 0
    assert chunk_datas[0]["content_with_weight"] == "old content"
