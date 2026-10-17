import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

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


async def test_agent_retrieval_preserves_dataset_tenant_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    import agent.tools.retrieval as module

    param = RetrievalParam()
    retrieval = build_retrieval(param)
    rows = [
        SimpleNamespace(id=identifier, tenant_id=owner, name="name-" + identifier, embd_id="embedding", tenant_embd_id=None) for identifier, owner in (("a", "owner"), ("b", "owner"), ("c", "other"))
    ]
    retrieval._canvas = SimpleNamespace(get_tenant_id=lambda: "owner")
    monkeypatch.setattr(retrieval, "_resolve_kbs", lambda: (["a", "b", "c"], ["a", "b", "c"], rows))
    monkeypatch.setattr(retrieval, "get_input_elements_from_text", lambda _: {})
    monkeypatch.setattr(retrieval, "string_format", lambda query, _: query)
    monkeypatch.setattr(retrieval, "_rank_feature", lambda *_: {})
    monkeypatch.setattr(module, "build_named_bundle_async", AsyncMock(return_value=object()))
    fetch = AsyncMock(return_value={"chunks": [], "doc_aggs": []})
    monkeypatch.setattr(module.settings, "retriever", SimpleNamespace(retrieval=fetch, retrieval_by_children=lambda chunks, _: chunks))
    await retrieval._retrieve_kb("query")
    assert fetch.call_args.args[3:5] == (["owner", "owner", "other"], ["name-a", "name-b", "name-c"])
    assert fetch.call_args.kwargs["kb_ids"] == ["a", "b", "c"]


async def test_agent_typed_manual_filter_skips_metadata_aggregation(db: "Session", monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager
    from unittest.mock import AsyncMock, Mock

    import agent.tools.retrieval as module

    param = RetrievalParam()
    param.meta_data_filter = {"method": "manual", "manual": [{"key": "product", "op": "in", "value": [0, False, "F2"]}]}
    retrieval = build_retrieval(param)
    rows = [SimpleNamespace(id="dataset", tenant_id="owner", name="name", embd_id="embedding", tenant_embd_id=None)]
    retrieval._canvas = SimpleNamespace(get_tenant_id=lambda: "owner")
    monkeypatch.setattr(retrieval, "_resolve_kbs", lambda: (["dataset"], ["dataset"], rows))
    monkeypatch.setattr(retrieval, "get_input_elements_from_text", lambda _: {})
    monkeypatch.setattr(retrieval, "string_format", lambda query, _: query)
    monkeypatch.setattr(retrieval, "_rank_feature", lambda *_: {})
    load = Mock(side_effect=AssertionError("Manual push-down must not aggregate metadata"))
    monkeypatch.setattr(retrieval, "_flatted_metas", load)
    pushed = Mock(return_value=["selected"])
    monkeypatch.setattr(module.DocMetadataService, "filter_doc_ids_by_meta_pushdown", pushed)

    @contextmanager
    def session() -> Iterator[Session]:
        yield db

    monkeypatch.setattr(module, "db_connection", session)
    monkeypatch.setattr(module, "build_named_bundle_async", AsyncMock(return_value=object()))
    fetch = AsyncMock(return_value={"chunks": [], "doc_aggs": []})
    monkeypatch.setattr(module.settings, "retriever", SimpleNamespace(retrieval=fetch, retrieval_by_children=lambda chunks, _: chunks))
    await retrieval._retrieve_kb("query")
    assert pushed.call_args.args[1:3] == (["dataset"], param.meta_data_filter["manual"])
    assert fetch.call_args.kwargs["doc_ids"] == ["selected"]
    load.assert_not_called()
