import asyncio
from types import SimpleNamespace

from agent.tools.retrieval import Retrieval, RetrievalParam


def build_retrieval(param):
    retrieval = object.__new__(Retrieval)
    retrieval._param = param
    retrieval.outputs = {}
    retrieval.check_if_canceled = lambda _message: False
    retrieval.set_output = lambda key, value: retrieval.outputs.__setitem__(key, value)
    return retrieval


def test_dataset_ids_prefer_new_field_over_legacy_kb_ids():
    param = RetrievalParam()
    param.dataset_ids = ["dataset-1"]
    param.kb_ids = ["legacy-kb"]
    retrieval = build_retrieval(param)

    assert retrieval._dataset_ids == ["dataset-1"]


def test_dataset_ids_fall_back_to_legacy_kb_ids():
    param = RetrievalParam()
    param.kb_ids = ["legacy-kb"]
    retrieval = build_retrieval(param)

    assert retrieval._dataset_ids == ["legacy-kb"]


def test_dataset_ids_fall_back_when_new_field_is_absent():
    param = SimpleNamespace(kb_ids=["legacy-kb"])
    retrieval = build_retrieval(param)

    assert retrieval._dataset_ids == ["legacy-kb"]


def test_invoke_uses_dataset_ids_for_dataset_retrieval(monkeypatch):
    called = {}
    param = SimpleNamespace(
        dataset_ids=["dataset-1"],
        kb_ids=[],
        memory_ids=[],
        retrieval_from=None,
        empty_response="",
    )
    retrieval = build_retrieval(param)

    async def fake_retrieve_kb(self, query):
        called["query"] = query
        return "retrieved"

    monkeypatch.setattr(Retrieval, "_retrieve_kb", fake_retrieve_kb)

    result = asyncio.run(retrieval._invoke_async(query="hello"))

    assert result == "retrieved"
    assert called == {"query": "hello"}
