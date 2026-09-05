"""EngineMetadataStore.upsert 的 doc-store 写形状。

要点是 ES 分支必须整体替换 meta_fields。`es.update(doc=...)` 对 object 字段做的是
深合并：用户删掉的键会在索引里活下来，元数据筛选随后按幽灵键把文档筛出来。
这个坏行为在打桩测试里看不出差别（两种写法都"调用成功"），只能靠钉住请求形状。
"""

import types

import pytest

from api.db.services.metadata_store_engine import EngineMetadataStore
from common import settings


@pytest.fixture
def es_store(monkeypatch):
    """ES 后端 + 记录所有 es.update 调用的 docStoreConn 假件。"""
    calls: list[dict] = []
    inserted: list[dict] = []

    monkeypatch.setattr(settings, "DOC_ENGINE_INFINITY", False)
    monkeypatch.setattr(settings, "DOC_ENGINE_OCEANBASE", False)
    monkeypatch.setattr(
        settings,
        "docStoreConn",
        types.SimpleNamespace(
            index_exist=lambda idx, kb_id: True,
            get=lambda doc_id, idx, kb_ids: {"id": doc_id},
            es=types.SimpleNamespace(
                update=lambda **kw: calls.append(kw),
                indices=types.SimpleNamespace(refresh=lambda index: None),
            ),
            insert=lambda docs, idx, kb_id: inserted.append(docs) or None,
        ),
    )
    return EngineMetadataStore(), calls, inserted


def test_upsert_replaces_meta_fields_instead_of_merging(db, es_store):
    store, calls, _ = es_store

    assert store.upsert(db, "d1", "t1", "kb1", {"author": "ada"}) is True

    assert len(calls) == 1
    call = calls[0]
    assert call["index"] == "multirag_doc_meta_t1"
    assert call["id"] == "d1"
    assert call["refresh"] is True
    # 深合并的 doc= 形态会让删掉的键留在索引里，必须走 script 整体赋值
    assert "doc" not in call
    assert call["script"] == {
        "source": "ctx._source.meta_fields = params.meta_fields",
        "params": {"meta_fields": {"author": "ada"}},
    }


def test_upsert_clears_all_fields_when_given_an_empty_map(db, es_store):
    """清空元数据是删除语义：脚本参数得是空 map，而不是被跳过。"""
    store, calls, _ = es_store

    assert store.upsert(db, "d1", "t1", "kb1", {}) is True

    assert calls[0]["script"]["params"] == {"meta_fields": {}}


def test_upsert_inserts_when_the_document_is_absent(db, es_store, monkeypatch):
    store, calls, inserted = es_store
    monkeypatch.setattr(settings.docStoreConn, "get", lambda doc_id, idx, kb_ids: None)

    assert store.upsert(db, "d1", "t1", "kb1", {"author": "ada"}) is True

    assert calls == []
    assert inserted == [[{"id": "d1", "kb_id": "kb1", "meta_fields": {"author": "ada"}}]]
