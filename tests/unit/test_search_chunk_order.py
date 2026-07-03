import asyncio

from core.nlp.search import Dealer


class _FakeDataStore:
    def __init__(self):
        self.search_call = None

    def db_type(self):
        return "infinity"

    def search(self, src, highlight_fields, filters, match_exprs, order_by, offset, limit, idx_names, kb_ids):
        self.search_call = {
            "src": src,
            "highlight_fields": highlight_fields,
            "filters": filters,
            "match_exprs": match_exprs,
            "order_by": order_by,
            "offset": offset,
            "limit": limit,
            "idx_names": idx_names,
            "kb_ids": kb_ids,
        }
        return {}

    def get_total(self, _results):
        return 0

    def get_doc_ids(self, _results):
        return []

    def get_highlight(self, _results, _keywords, _field):
        return {}

    def get_aggregation(self, _results, _field):
        return {}

    def get_fields(self, _results, _fields):
        return {}


def test_empty_query_search_defaults_include_chunk_order_sorting():
    data_store = _FakeDataStore()
    dealer = Dealer.__new__(Dealer)
    dealer.dataStore = data_store

    asyncio.run(
        dealer.search(
            {"question": "", "sort": True},
            idx_names=["idx"],
            kb_ids=["kb-1"],
        )
    )

    assert "chunk_order_int" in data_store.search_call["src"]
    assert data_store.search_call["order_by"].fields[:4] == [
        ("chunk_order_int", 0),
        ("page_num_int", 0),
        ("top_int", 0),
        ("create_timestamp_flt", 1),
    ]
