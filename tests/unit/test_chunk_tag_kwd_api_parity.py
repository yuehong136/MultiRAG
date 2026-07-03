import asyncio
import types

from core.nlp import search as search_module


class _FakeDataStore:
    def db_type(self):
        return "milvus"


def test_retrieval_exposes_tag_kwd_from_search_result(monkeypatch):
    monkeypatch.setattr(search_module.settings, "DOC_ENGINE_INFINITY", False)
    dealer = object.__new__(search_module.Dealer)
    dealer.dataStore = _FakeDataStore()

    async def fake_search(*args, **kwargs):
        return search_module.Dealer.SearchResult(
            total=1,
            ids=["chunk-1"],
            query_vector=[0.1, 0.2],
            field={
                "chunk-1": {
                    "_score": 0.9,
                    "content_ltks": "tagged content",
                    "content_with_weight": "tagged content",
                    "doc_id": "doc-1",
                    "docnm_kwd": "doc.txt",
                    "kb_id": "kb-1",
                    "important_kwd": ["important"],
                    "tag_kwd": ["finance", "approved"],
                    "q_2_vec": [0.1, 0.2],
                }
            },
            highlight={},
        )

    dealer.search = types.MethodType(fake_search, dealer)

    ranks = asyncio.run(
        dealer.retrieval(
            question="tagged",
            filter_exp=None,
            embd_mdl=None,
            tenant_id="tenant-1",
            kb_names=["dataset"],
            page=1,
            page_size=10,
            similarity_threshold=0.1,
            rank_feature={},
        )
    )

    assert ranks["chunks"][0]["tag_kwd"] == ["finance", "approved"]
