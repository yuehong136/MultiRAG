"""Controlled RPC contract for the actual scoped adapter and managed store.

Rows stay fresh even when preparation is reused. Real Milvus performance and
concurrent collection recreation are covered by the separately owned suite.
"""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest

from common import settings
from memory.revisions.domain import RevisionValidationError
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import SourceMessageBindingError
from memory.utils import es_conn, infinity_conn, milvus_conn
from tests.unit.test_memory_strict_storage_contract import BIG_ID, connection, document
from tests.unit.test_memory_strict_storage_contract import native as native

Native = tuple[Any, dict[str, Any]]


async def test_prepared_store_reads_keep_full_strong_scalar_identity_contract(native: Native, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, state = native
    store = ManagedMessageStore("tenant", "bank")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("A prepared read must use its scoped query, not prepare again")

    monkeypatch.setattr(adapter, "query_source_rows", forbidden)
    async with store.prepared_read_pass():
        assert state["calls"] == []
        first = await store.read(BIG_ID)
        state["rows"][0]["content_ltks"] = "下一次实际读取；仅限测试。"
        second = await store.read(BIG_ID)
    assert first is not None and second is not None
    assert first["content"] == "完整条件；禁止生产。" and second["content"] == "下一次实际读取；仅限测试。"
    assert second["message_id"] == BIG_ID and type(second["message_id"]) is int
    assert second["content_embed"] == [0.125, 0.75] and second["status"] is False
    assert second["forget_at"] == "2026-11-16 10:00:00" and second["invalid_at"] == "2026-11-16 09:30:00"
    assert (second["user_id"], second["agent_id"], second["session_id"]) == ("", "", "")
    calls = state["calls"]
    assert [call[0] for call in calls] == ["exists", "collection", "load", "query", "query"]
    assert calls[0][1:] == ("memory_tenant_bank", "strict-storage-contract")
    for _, query in calls[3:]:
        assert query["expr"] == f'memory_id == "bank" and message_id in [{BIG_ID}]'
        assert query["consistency_level"] == "Strong" and query["limit"] == 2
        assert "order_by" not in query and "offset" not in query
        assert {"id", "memory_id", "message_id", "source_id", "status_int", "forget_at", "valid_at", "invalid_at", "content_ltks", "q_2_vec"} <= set(query["output_fields"])


async def test_new_pass_reconstructs_public_collection_and_vector_field_plan(native: Native, monkeypatch: pytest.MonkeyPatch) -> None:
    _, state = native
    original = milvus_conn.Collection

    class Collection:
        def __init__(self, name: str, *, using: str) -> None:
            self._original = original(name, using=using)
            self.schema = SimpleNamespace(fields=[SimpleNamespace(name=key) for key in state["rows"][0]])

        def load(self) -> None:
            self._original.load()

        def query(self, **kwargs: Any) -> list[dict[str, Any]]:
            return cast(list[dict[str, Any]], self._original.query(**kwargs))

    monkeypatch.setattr(milvus_conn, "Collection", Collection)
    store = ManagedMessageStore("tenant", "bank")
    async with store.prepared_read_pass():
        first = await store.read(BIG_ID)
    current = deepcopy(state["rows"][0])
    del current["q_2_vec"]
    current["q_3_vec"] = [0.25, 0.5, 0.75]
    state["rows"] = [current]
    async with store.prepared_read_pass():
        second = await store.read(BIG_ID)
    assert first is not None and second is not None
    assert first["content_embed"] == [0.125, 0.75] and second["content_embed"] == [0.25, 0.5, 0.75]
    assert sum(call[0] == "collection" for call in state["calls"]) == 2
    assert sum(call[0] == "load" for call in state["calls"]) == 2
    queries = [call[1] for call in state["calls"] if call[0] == "query"]
    assert "q_2_vec" in queries[0]["output_fields"] and "q_3_vec" not in queries[0]["output_fields"]
    assert "q_3_vec" in queries[1]["output_fields"] and "q_2_vec" not in queries[1]["output_fields"]


async def test_alias_appearing_after_preparation_is_not_hidden_by_cached_rows(native: Native) -> None:
    _, state = native
    store = ManagedMessageStore("tenant", "bank")
    async with store.prepared_read_pass():
        assert await store.read(BIG_ID) is not None
        state["rows"].append(document(id="legacy-alias"))
        with pytest.raises(SourceMessageBindingError):
            await store.read(BIG_ID)
    queries = [call[1] for call in state["calls"] if call[0] == "query"]
    assert len(queries) == 2 and all(query["limit"] == 2 for query in queries)


@pytest.mark.parametrize("malformed", [document(memory_id="other"), document(id="other_1"), document(message_id=BIG_ID + 1)])
async def test_prepared_rows_still_require_exact_owner_and_requested_identity(native: Native, malformed: dict[str, Any]) -> None:
    _, state = native
    state["rows"] = [malformed]
    store = ManagedMessageStore("tenant", "bank")
    async with store.prepared_read_pass():
        with pytest.raises((SourceMessageBindingError, RevisionValidationError)):
            await store.read(BIG_ID)


async def test_actual_prepared_query_error_cannot_become_missing(native: Native, monkeypatch: pytest.MonkeyPatch) -> None:
    _, state = native

    def query(self: Any, **kwargs: Any) -> list[dict[str, Any]]:
        state["calls"].append(("query-error", deepcopy(kwargs)))
        raise TimeoutError("controlled storage failure")

    monkeypatch.setattr(milvus_conn.Collection, "query", query)
    store = ManagedMessageStore("tenant", "bank")
    async with store.prepared_read_pass():
        with pytest.raises(TimeoutError, match="controlled storage failure"):
            await store.read(BIG_ID)


async def test_confirmed_absence_is_not_a_pass_wide_negative_cache(native: Native) -> None:
    _, state = native
    state["present"] = False
    store = ManagedMessageStore("tenant", "bank")
    async with store.prepared_read_pass():
        assert await store.read(BIG_ID) is None
        state["present"] = True
        assert await store.read(BIG_ID) is not None
    assert [call[0] for call in state["calls"]] == ["exists", "exists", "collection", "load", "query"]


@pytest.mark.parametrize("factory", [es_conn.ESConnection, infinity_conn.InfinityConnection])
async def test_optional_preparation_does_not_grant_unsupported_backends_strict_reads(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    adapter = connection(factory)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("A legacy getter is not a strict storage fallback")

    monkeypatch.setattr(adapter, "get", forbidden)
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", adapter)
    store = ManagedMessageStore("tenant", "bank")
    async with store.prepared_read_pass():
        with pytest.raises(RevisionValidationError, match="managed_backend_unsupported"):
            await store.read(BIG_ID)
