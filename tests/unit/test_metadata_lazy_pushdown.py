"""Lazy reads, exact expanded-value semantics, and fail-closed storage IO."""

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from api.db.services.metadata_store_engine import EngineMetadataStore
from common import settings
from common.doc_store.search_metadata import SearchMetadata
from common.metadata_es_filter import UnsupportedMetaFilter, build_meta_filter_query
from common.metadata_utils import aggregate_metadata_rows, apply_meta_data_filter, meta_filter, stream_metadata_matches


@pytest.mark.parametrize("config", [None, {}, {"method": "manual", "manual": []}, {"method": "unknown"}, {"method": "manual", "manual": [{"key": "x", "op": "not in", "value": [None]}]}])
async def test_noop_or_invalid_conditions_never_load_or_filter(config: dict[str, Any] | None) -> None:
    load, push = AsyncMock(), AsyncMock()
    result = await apply_meta_data_filter(config, base_doc_ids=["selected"], metas_loader=load, metadata_filterer=push)
    assert result == (["-999"] if config and config.get("manual") else ["selected"])
    load.assert_not_awaited()
    push.assert_not_awaited()


@pytest.mark.parametrize("pushed,expected", [(["selected", "outside"], ["selected"]), ([], ["-999"]), (None, ["selected"])])
async def test_pushdown_success_empty_and_explicit_fallback(pushed: list[str] | None, expected: list[str]) -> None:
    load = AsyncMock(return_value={"product": {"F2": ["selected", "outside"]}})
    push = AsyncMock(return_value=pushed)
    config = {"method": "manual", "manual": [{"key": "product", "op": "in", "value": ["f2"]}]}
    assert await apply_meta_data_filter(config, base_doc_ids=["selected"], metas_loader=load, metadata_filterer=push) == expected
    assert load.await_count == (1 if pushed is None else 0)
    push.assert_awaited_once()


async def test_rpc_failure_does_not_fall_back_or_become_empty_success() -> None:
    load = AsyncMock()
    push = AsyncMock(side_effect=ConnectionError("owned ES unavailable"))
    with pytest.raises(ConnectionError):
        await apply_meta_data_filter({"method": "manual", "manual": [{"key": "x", "op": "in", "value": [0, False]}]}, metas_loader=load, metadata_filterer=push)
    load.assert_not_awaited()


async def test_auto_loader_is_memoized_and_semi_auto_uses_fresh_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    generated = AsyncMock(return_value={"conditions": [{"key": "product", "op": "in", "value": ["F2"]}]})
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", generated)
    load = AsyncMock(return_value={"product": {"f2": ["selected"]}})
    push = AsyncMock(return_value=None)
    assert await apply_meta_data_filter({"method": "auto"}, metas_loader=load, metadata_filterer=push) == ["selected"]
    load.assert_awaited_once()
    push.reset_mock()
    refresh = AsyncMock(return_value={})
    assert await apply_meta_data_filter({"method": "semi_auto", "semi_auto": ["product"]}, metas_loader=load, metadata_filterer=push, metadata_refresher=refresh) == ["-999"]
    push.assert_not_awaited()


@pytest.mark.parametrize("operator", ["in", "not in", "contains", "not contains", "start with", "end with", "empty", "not empty"])
@pytest.mark.parametrize("logic", ["and", "or"])
def test_streaming_result_sets_match_complete_typed_aggregation(operator: str, logic: str) -> None:
    rows = [
        ("a", {"value": ["F2", 0, False], "status": "ready"}),
        ("b", {"value": ["f11", "0", "False"], "status": "ready"}),
        ("c", {"value": ["", None, "ÄBC", "K"]}),
        ("missing", {"status": "ready"}),
        ("empty", {"value": []}),
    ]
    for value in (["F2", 0, False], ["0", "FALSE"], [], "f2,f11", "", "Ä", "k", "0", "false"):
        conditions = [{"key": "value", "op": operator, "value": value}, {"key": "status", "op": "in", "value": ["ready"]}]
        before = deepcopy((rows, conditions))
        assert set(stream_metadata_matches(iter(rows), conditions, logic)) == set(meta_filter(aggregate_metadata_rows(rows), conditions, logic))
        assert (rows, conditions) == before


@pytest.mark.parametrize("field", [{"type": "keyword", "ignore_above": 256}, {"type": "long", "ignore_malformed": True}, {"type": "keyword", "index": False}, {"type": "object"}, {}])
def test_candidate_compiler_declines_lossy_or_unknown_mappings(field: dict[str, Any]) -> None:
    mapping = {"properties": {"meta_fields": {"properties": {"value": field}}}}
    with pytest.raises(UnsupportedMetaFilter):
        build_meta_filter_query([{"key": "value", "op": "in", "value": [False]}], "and", ["kb"], mapping)


@pytest.mark.parametrize("failure", ["rpc", "shard", "timeout", "scope"])
def test_scroll_partial_failures_propagate_and_clear_cursor(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    page = {"_scroll_id": "owned", "timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [{"_id": "doc", "_source": {"kb_id": "kb", "meta_fields": {"x": 0}}}]}}
    next_page: dict[str, Any] = {"_scroll_id": "owned", "timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": []}}
    if failure == "shard":
        next_page["_shards"] = {"failed": 1}
    if failure == "timeout":
        next_page["timed_out"] = True
    if failure == "scope":
        page["hits"]["hits"][0]["_source"]["kb_id"] = "foreign"
    client = Mock()
    client.search.return_value = page
    client.scroll.return_value = next_page
    if failure == "rpc":
        client.scroll.side_effect = ConnectionError("owned failure")
    connector = SearchMetadata()
    monkeypatch.setattr(connector, "_doc_meta_client", lambda: client)
    monkeypatch.setattr(settings, "docStoreConn", connector)
    with pytest.raises((ConnectionError, RuntimeError)):
        list(EngineMetadataStore._iter_es_rows("owned", ["kb"], {"match_all": {}}))
    client.clear_scroll.assert_called_once_with(scroll_id="owned")
    assert client.search.call_args.kwargs["body"]["sort"] == ["id"]


def test_matcher_exception_closes_source_iterator() -> None:
    closed = []

    def rows() -> Any:
        try:
            yield "doc", {"x": "value"}
        finally:
            closed.append(True)

    with pytest.raises(TypeError):
        stream_metadata_matches(rows(), [{"key": [], "op": "in", "value": ["value"]}], "and")
    assert closed == [True]
