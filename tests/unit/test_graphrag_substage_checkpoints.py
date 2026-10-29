"""Resume successful model work without trusting foreign or malformed results."""

import asyncio
import json
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import networkx as nx
import pytest

from core.graphrag import checkpoints
from core.graphrag.checkpoints import StageCheckpoints, community_checkpoint_key, fingerprint, resolution_checkpoint_key, stage_revision
from core.graphrag.entity_resolution import EntityResolution
from core.graphrag.general import community_reports_extractor as community
from core.graphrag.general import extractor


class Model:
    llm_name = "controlled-model"
    max_length = 8192
    model_config = {"id": 1, "provider_instance_id": 2, "api_base": "http://controlled.invalid", "max_tokens": 8192}

    async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> str:
        return "<|>1<|>&&No&&"


def graph() -> nx.Graph:
    result = nx.Graph()
    for name, kind in [("ALPHA", "person"), ("ALPHAS", "person"), ("BETA", "organization"), ("BETAS", "organization")]:
        result.add_node(name, entity_type=kind, description=name, source_id=["doc"])
    result.add_edge("ALPHA", "ALPHAS", description="related", weight=1)
    result.add_edge("BETA", "BETAS", description="related", weight=1)
    return result


@pytest.fixture
def model_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor, "get_llm_cache", lambda *args: None)
    monkeypatch.setattr(extractor, "set_llm_cache", lambda *args: None)


def report(title: str = "title") -> dict[str, Any]:
    return {"title": title, "summary": "summary", "findings": [{"summary": "finding", "explanation": "evidence"}], "rating": 1.0, "rating_explanation": "reason"}


@pytest.mark.parametrize("change", ["graph", "config", "model", "endpoint", "options"])
def test_input_changes_invalidate_scope(change: str) -> None:
    original, config, model = graph(), {"graphrag": {"resolution": True}}, Model()
    model.model_config = dict(model.model_config)
    before = stage_revision(original, config, model)
    if change == "graph":
        original.nodes["ALPHA"]["description"] = "changed"
    elif change == "config":
        config["graphrag"]["resolution"] = False
    elif change == "model":
        model.llm_name = "new-model"
    elif change == "endpoint":
        model.model_config["api_base"] = "http://new.invalid"
    else:
        model.model_config["max_tokens"] = 4096
    assert stage_revision(original, config, model) != before


def test_graph_roundtrip_order_does_not_invalidate_scope() -> None:
    original = graph()
    reordered = nx.Graph()
    reordered.add_nodes_from(reversed(list(original.nodes(data=True))))
    reordered.add_edges_from((b, a, data) for a, b, data in reversed(list(original.edges(data=True))))
    assert stage_revision(original, {}, Model()) == stage_revision(reordered, {}, Model())


async def test_resolution_resumes_valid_empty_batch_and_recomputes_foreign_pairs(model_boundary: None, monkeypatch: pytest.MonkeyPatch) -> None:
    model, cached = Model(), {}
    saved = asyncio.Event()
    calls: list[str] = []
    fail = True

    async def chat(system: str, *args: Any) -> str:
        calls.append(system)
        if "person A is ALPHA" in system:
            return "<|>1<|>&&No&&"
        await saved.wait()
        if fail:
            raise TimeoutError("injected failure after successful first batch")
        return "<|>1<|>&&No&&"

    async def save(key: str, payload: Any) -> None:
        cached[key] = deepcopy(payload)
        saved.set()

    monkeypatch.setattr(model, "async_chat", chat)
    er = EntityResolution(model)
    with pytest.raises(TimeoutError):
        await er(graph(), set(graph()), callback=lambda **kwargs: None, checkpoints=cached, save_checkpoint=save)
    assert len(calls) == 2 and list(cached.values()) == [[]]
    fail = False
    await er(graph(), set(graph()), callback=lambda **kwargs: None, checkpoints=cached, save_checkpoint=save)
    assert len(calls) == 3  # Successful No/empty selection was durably replayed.
    cached[resolution_checkpoint_key("person", [("ALPHA", "ALPHAS")])] = [["ALPHA", "FOREIGN"]]
    await er(graph(), set(graph()), callback=lambda **kwargs: None, checkpoints=cached, save_checkpoint=save)
    assert len(calls) == 4


async def test_community_replay_uses_current_membership_and_copies_cache(model_boundary: None, monkeypatch: pytest.MonkeyPatch) -> None:
    communities = {"0": {"a": {"nodes": ["ALPHA", "ALPHAS"], "weight": 7}}}
    monkeypatch.setattr(community.leiden, "run", lambda *args: deepcopy(communities))
    key = community_checkpoint_key("0", "a", ["ALPHA", "ALPHAS"])
    payload = {**report(), "entities": ["FOREIGN"], "weight": -1}
    cached = {key: payload}
    before = deepcopy(cached)
    model = Model()
    monkeypatch.setattr(model, "async_chat", AsyncMock(side_effect=AssertionError("replay called model")))
    result = await community.CommunityReportsExtractor(model)(graph(), checkpoints=cached)
    assert result.structured_output[0]["entities"] == ["ALPHA", "ALPHAS"]
    assert result.structured_output[0]["weight"] == 7 and cached == before
    result.structured_output[0]["findings"][0]["summary"] = "mutated"
    assert cached == before


@pytest.mark.parametrize("invalid", [None, {}, {**report(), "findings": [1]}, {**report(), "summary": None}])
async def test_bad_community_payload_recomputes(model_boundary: None, monkeypatch: pytest.MonkeyPatch, invalid: Any) -> None:
    monkeypatch.setattr(community.leiden, "run", lambda *args: {"0": {"a": {"nodes": ["ALPHA", "ALPHAS"], "weight": 1}}})
    model = Model()
    call = AsyncMock(return_value=json.dumps(report()))
    monkeypatch.setattr(model, "async_chat", call)
    key = community_checkpoint_key("0", "a", ["ALPHA", "ALPHAS"])
    await community.CommunityReportsExtractor(model)(graph(), checkpoints={key: invalid})
    call.assert_awaited_once()


@pytest.mark.parametrize("operation", ["load", "save", "cleanup"])
async def test_redis_outage_propagates(operation: str, monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.sscan.side_effect = ConnectionError("offline")
    client.pipeline.side_effect = ConnectionError("offline")
    monkeypatch.setattr(checkpoints.REDIS_CONN, "REDIS", client)
    cache = StageCheckpoints("tenant", "kb", "resolution", fingerprint("revision"))
    with pytest.raises(ConnectionError, match="offline"):
        if operation == "save":
            await cache.save(fingerprint("batch"), [])
        else:
            await getattr(cache, operation)()


async def test_paged_load_rejects_corrupt_expired_and_foreign_envelopes(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = StageCheckpoints("tenant", "kb", "resolution", fingerprint("revision"))
    keys = [fingerprint(i) for i in range(4)]
    client = MagicMock()
    client.sscan.side_effect = [(1, [keys[0], "foreign:raw-key", keys[1]]), (0, [keys[2], keys[3]])]
    client.mget.side_effect = [
        [json.dumps({"revision": cache.revision, "scope": cache.index_key, "key": keys[0], "payload": []}), "invalid JSON"],
        [None, json.dumps({"revision": cache.revision, "scope": "foreign", "key": keys[3], "payload": []})],
    ]
    monkeypatch.setattr(checkpoints.REDIS_CONN, "REDIS", client)
    assert await cache.load(page_size=2) == {keys[0]: []}
    assert client.sscan.call_count == 2 and client.mget.call_count == 2
    assert StageCheckpoints("other", "kb", "resolution", cache.revision).index_key != cache.index_key
    assert StageCheckpoints("tenant", "other", "resolution", cache.revision).index_key != cache.index_key


def test_database_model_shape_ignores_usage_and_audit_dates() -> None:
    from datetime import datetime

    model = Model()
    model.model_config = {**model.model_config, "create_date": datetime(2026, 1, 1), "update_date": datetime(2026, 1, 2), "used_tokens": 17, "update_time": 12345}
    before = stage_revision(graph(), {}, model)
    model.model_config.update(used_tokens=190, update_date=datetime(2026, 2, 1), update_time=54321)
    assert stage_revision(graph(), {}, model) == before
    model.model_config["instance_id"] = 55
    assert stage_revision(graph(), {}, model) != before


@pytest.mark.parametrize("response", ["garbled", "<|>1<|>&&Maybe&&", "<|>2<|>&&No&&", "<|>1<|>&&No&&##<|>1<|>&&Yes&&", "<|>1<|>&&No&&"])
async def test_incomplete_or_malformed_resolution_is_not_saved(model_boundary: None, monkeypatch: pytest.MonkeyPatch, response: str) -> None:
    model = Model()
    call = AsyncMock(return_value=response)
    monkeypatch.setattr(model, "async_chat", call)
    er = EntityResolution(model, use_llm_cache=False)
    er.prompt_variables = {}
    saved = AsyncMock()
    # Two questions: even a valid single No is an incomplete batch.
    with pytest.raises(ValueError):
        result = await er._resolve_candidate(("person", [("ALPHA", "ALPHAS"), ("BETA", "BETAS")]), set(), asyncio.Lock())
        await saved("key", result)
    saved.assert_not_awaited()


async def test_validated_stages_do_not_replay_unvalidated_raw_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    model = Model()
    call = AsyncMock(return_value="<|>1<|>&&No&&")
    monkeypatch.setattr(model, "async_chat", call)
    raw_get, raw_set = MagicMock(return_value="garbled"), MagicMock()
    monkeypatch.setattr(extractor, "get_llm_cache", raw_get)
    monkeypatch.setattr(extractor, "set_llm_cache", raw_set)
    await EntityResolution(model, use_llm_cache=False)(graph(), set(graph()), callback=lambda **kwargs: None)
    assert call.await_count == 2
    raw_get.assert_not_called()
    raw_set.assert_not_called()


async def test_entry_envelope_cannot_be_swapped_within_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = StageCheckpoints("tenant", "kb", "community", fingerprint("revision"))
    first, second = fingerprint("first"), fingerprint("second")
    client = MagicMock()
    client.sscan.return_value = (0, [first])
    client.mget.return_value = [json.dumps({"revision": cache.revision, "scope": cache.index_key, "key": second, "payload": report("foreign")})]
    monkeypatch.setattr(checkpoints.REDIS_CONN, "REDIS", client)
    assert await cache.load() == {}


@pytest.mark.parametrize("stage", ["resolution", "community"])
async def test_cancellation_drains_all_model_children(model_boundary: None, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    started, settled = asyncio.Event(), []
    model = Model()

    async def chat(system: str, *args: Any) -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.append(system)
        return "unreachable"

    monkeypatch.setattr(model, "async_chat", chat)
    if stage == "community":
        monkeypatch.setattr(community.leiden, "run", lambda *args: {"0": {"a": {"nodes": ["ALPHA", "ALPHAS"], "weight": 1}, "b": {"nodes": ["BETA", "BETAS"], "weight": 1}}})
        operation = community.CommunityReportsExtractor(model)(graph())
    else:
        operation = EntityResolution(model)(graph(), set(graph()), callback=lambda **kwargs: None)
    task = asyncio.create_task(operation)
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(settled) == 2
