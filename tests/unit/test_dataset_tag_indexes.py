"""Exercise the real tag retriever against per-dataset index boundaries."""

from contextlib import contextmanager
from types import SimpleNamespace

from api.db.db_models import Knowledgebase
from api.db.services.knowledgebase_service import KnowledgebaseService
from core.nlp import search


def test_all_tags_queries_every_owned_dataset_and_merges_counts(monkeypatch):
    kbs = [Knowledgebase(id="a", name="A", tenant_id="t1"), Knowledgebase(id="b", name="B", tenant_id="t1"), Knowledgebase(id="c", name="C", tenant_id="t2")]

    @contextmanager
    def connection():
        yield None

    monkeypatch.setattr(search, "db_connection", connection)
    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", classmethod(lambda cls, db, ids: kbs))
    seen = []

    def query(*args):
        seen.append((args[7], args[8]))
        return args[8][0]

    store = SimpleNamespace(index_exist=lambda idx, kb: True, search=query, get_aggregation=lambda res, field: [("shared", 2)] if res == "a" else [("shared", 3), ("only-b", 1)])
    retriever = object.__new__(search.Dealer)
    retriever.dataStore = store
    assert retriever.all_tags("t1", ["a", "b", "c", "a"]) == [("shared", 5), ("only-b", 1)]
    assert seen == [(["multirag_t1_A"], ["a"]), (["multirag_t1_B"], ["b"])]


def test_all_tags_skips_missing_index_without_hiding_later_dataset(monkeypatch):
    @contextmanager
    def connection():
        yield None

    monkeypatch.setattr(search, "db_connection", connection)
    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", classmethod(lambda cls, db, ids: [Knowledgebase(id="a", name="A", tenant_id="t1"), Knowledgebase(id="b", name="B", tenant_id="t1")]))
    retriever = object.__new__(search.Dealer)
    retriever.dataStore = SimpleNamespace(index_exist=lambda idx, kb: kb == "b", search=lambda *args: args[8], get_aggregation=lambda result, field: [("found", 1)] if result == ["b"] else [])
    assert retriever.all_tags("t1", ["a", "b"]) == [("found", 1)]
    assert retriever.all_tags("t1", []) == []
