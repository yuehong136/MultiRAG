"""Real adapter/managed-store contract with controlled RPCs, no storage service.

Strong reads preserve lifecycle rows and exact int64 identity. These tests
do not establish a multi-request storage snapshot or backend throughput.
"""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from api.db.joint_services.memory_write_runtime import supports_coordinated_writes
from common import settings
from memory.revisions.domain import RevisionValidationError
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import SourceMessageBindingError
from memory.utils import es_conn, infinity_conn, milvus_conn

BIG_ID = 9007199254741093


def connection(factory: Any) -> Any:
    cls = next(cell.cell_contents for cell in factory.__closure__ if isinstance(cell.cell_contents, type))
    return object.__new__(cls)


def document(identity: int = BIG_ID, **changes: Any) -> dict[str, Any]:
    return {
        "id": f"bank_{identity}",
        "message_id": identity,
        "memory_id": "bank",
        "message_type_kwd": "semantic",
        "source_id": 7,
        "user_id": "",
        "agent_id": "",
        "session_id": "",
        "valid_at": "2026-11-16 09:00:00",
        "invalid_at": "2026-11-16 09:30:00",
        "forget_at": "2026-11-16 10:00:00",
        "status_int": 0,
        "content_ltks": "完整条件；禁止生产。",
        "q_2_vec": [0.125, 0.75],
        **changes,
    }


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, Any]]:
    adapter = connection(milvus_conn.MilvusConnection)
    adapter._using = "strict-storage-contract"
    state: dict[str, Any] = {"rows": [document()], "calls": [], "present": True, "failure": None}

    def exists(name: str, *, using: str) -> bool:
        state["calls"].append(("exists", name, using))
        if state["failure"] is not None:
            raise state["failure"]
        return state["present"]

    class Collection:
        def __init__(self, name: str, *, using: str) -> None:
            state["calls"].append(("collection", name, using))
            self.schema = SimpleNamespace(fields=[SimpleNamespace(name=field) for field in document()])

        def load(self) -> None:
            state["calls"].append(("load",))

        def query(self, **kwargs: Any) -> list[dict[str, Any]]:
            state["calls"].append(("query", deepcopy(kwargs)))
            return deepcopy(state["rows"][: kwargs.get("limit", len(state["rows"]))])

    monkeypatch.setattr(milvus_conn.utility, "has_collection", exists)
    monkeypatch.setattr(milvus_conn, "Collection", Collection)
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", adapter)
    return adapter, state


async def test_managed_native_read_uses_owner_routing_strong_scope_and_full_lifecycle_fields(native: tuple[Any, dict[str, Any]]) -> None:
    _, state = native
    result = await ManagedMessageStore("tenant", "bank").read(BIG_ID)
    assert result is not None and result["message_id"] == BIG_ID and type(result["message_id"]) is int
    assert result["content_embed"] == [0.125, 0.75] and result["content"] == "完整条件；禁止生产。"
    assert result["status"] is False and result["forget_at"] == "2026-11-16 10:00:00"
    assert (result["user_id"], result["agent_id"], result["session_id"]) == ("", "", "")
    assert result["invalid_at"] == "2026-11-16 09:30:00"
    assert state["calls"][:3] == [("exists", "memory_tenant_bank", "strict-storage-contract"), ("collection", "memory_tenant_bank", "strict-storage-contract"), ("load",)]
    query = state["calls"][3][1]
    assert query["expr"] == f'memory_id == "bank" and message_id in [{BIG_ID}]'
    assert query["consistency_level"] == "Strong" and query["limit"] == 2
    assert "order_by" not in query and "offset" not in query
    assert {"q_2_vec", "id", "message_id", "memory_id", "source_id", "content_ltks", "status_int", "forget_at", "valid_at", "invalid_at"} <= set(query["output_fields"])


async def test_each_pass_requeries_instead_of_caching_a_prior_physical_snapshot(native: tuple[Any, dict[str, Any]]) -> None:
    _, state = native
    store = ManagedMessageStore("tenant", "bank")
    before = await store.read(BIG_ID)
    state["rows"][0]["content_ltks"] = "来源已变；重新复核。"
    after = await store.read(BIG_ID)
    assert before is not None and after is not None
    assert before["content"] == "完整条件；禁止生产。" and after["content"] == "来源已变；重新复核。"
    assert len([call for call in state["calls"] if call[0] == "query"]) == 2


async def test_scalar_message_id_alias_cannot_be_hidden_by_a_one_row_limit(native: tuple[Any, dict[str, Any]]) -> None:
    _, state = native
    state["rows"] = [document(), document(id="legacy_alias")]
    with pytest.raises(SourceMessageBindingError, match="authorized memory"):
        await ManagedMessageStore("tenant", "bank").read(BIG_ID)


@pytest.mark.parametrize("malformed", [document(message_id=1), document(memory_id="foreign"), document(id="foreign_1"), document(message_id=float(BIG_ID))])
async def test_native_result_identity_is_checked_before_any_content_is_returned(native: tuple[Any, dict[str, Any]], malformed: dict[str, Any]) -> None:
    _, state = native
    state["rows"] = [malformed]
    with pytest.raises((SourceMessageBindingError, RevisionValidationError)):
        await ManagedMessageStore("tenant", "bank").read(BIG_ID)


async def test_real_absence_is_missing_but_metadata_rpc_failure_is_not(native: tuple[Any, dict[str, Any]]) -> None:
    _, state = native
    state["present"] = False
    assert await ManagedMessageStore("tenant", "bank").read(BIG_ID) is None
    assert not any(call[0] == "query" for call in state["calls"])
    state["failure"] = TimeoutError("metadata RPC unavailable")
    with pytest.raises(TimeoutError, match="metadata RPC unavailable"):
        await ManagedMessageStore("tenant", "bank").read(BIG_ID)


@pytest.mark.parametrize("ids", [[], [True], [0], [2**63], list(range(1, 102))])
def test_strict_native_query_rejects_invalid_or_unbounded_ids_before_rpc(native: tuple[Any, dict[str, Any]], ids: list[int]) -> None:
    adapter, state = native
    with pytest.raises(ValueError):
        adapter.query_source_rows(["id"], ids, "memory_tenant", "bank")
    assert state["calls"] == []


@pytest.mark.parametrize("factory", [es_conn.ESConnection, infinity_conn.InfinityConnection])
async def test_unsupported_backends_keep_gate_false_and_never_fall_back_to_legacy_get(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    adapter = connection(factory)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Strict managed reads must not call a legacy best-effort getter")

    monkeypatch.setattr(adapter, "get", forbidden)
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", adapter)
    assert not supports_coordinated_writes()
    with pytest.raises(RevisionValidationError, match="managed_backend_unsupported"):
        await ManagedMessageStore("tenant", "bank").read(BIG_ID)
