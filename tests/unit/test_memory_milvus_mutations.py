"""Memory replacements use one acknowledged upsert and never pre-delete old rows."""

import copy
import logging
from types import SimpleNamespace
from typing import Any

import grpc
import pytest
from pymilvus.client.grpc_handler import GrpcHandler
from pymilvus.decorators import retry_on_rpc_failure
from pymilvus.exceptions import MilvusException

from memory.utils import milvus_conn


def message(**changes: Any) -> dict[str, Any]:
    return {
        "id": "bank_1",
        "memory_id": "bank",
        "message_id": 1,
        "message_type": "raw",
        "source_id": 0,
        "user_id": "subject",
        "agent_id": "agent",
        "session_id": "session",
        "valid_at": "2026-11-15 10:00:00",
        "invalid_at": None,
        "forget_at": None,
        "status": True,
        "content": "original",
        "content_embed": [1.0, 0.0],
        **changes,
    }


class MutationCollection:
    def __init__(self) -> None:
        row = milvus_conn.MilvusConnection.__closure__
        cls = next(cell.cell_contents for cell in row or [] if isinstance(cell.cell_contents, type))
        original = cls.map_message_to_milvus_fields(message())
        original["extra_meta"] = {"keep": ["original", 42]}
        self.rows = {"bank_1": original}
        self.calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
        self.query_calls: list[dict[str, Any]] = []
        self.query_result: list[dict[str, Any]] | None = None
        self.write_error: Exception | None = None
        self.commit_then_error = False
        self.count: Any = 1
        self.before_upsert: Any = None

    def load(self) -> None:
        pass

    def query(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.query_calls.append(copy.deepcopy(kwargs))
        rows = self.query_result if self.query_result is not None else list(self.rows.values())
        if kwargs["output_fields"] == ["*"]:
            return copy.deepcopy(rows)
        return [{field: row[field] for field in kwargs["output_fields"] if field in row} for row in copy.deepcopy(rows)]

    def upsert(self, rows: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append((copy.deepcopy(rows), copy.deepcopy(kwargs)))
        assert kwargs["retry_times"] == 0 and kwargs["retry_on_rate_limit"] is False
        assert "timeout" not in kwargs  # A numeric SDK timeout would override the retry count.
        if self.write_error is not None:
            raise self.write_error
        if self.before_upsert is not None:
            self.before_upsert(self.rows)
        replacement = copy.deepcopy(self.rows)
        for row in rows:
            if kwargs["partial_update"] and row["id"] in replacement:
                merged = {**replacement[row["id"]], **copy.deepcopy(row)}
            else:
                merged = copy.deepcopy(row)
            if not isinstance(merged.get("content_ltks"), str) or len(merged.get("q_2_vec", [])) != 2:
                raise ValueError("SDK validation rejected an incomplete or malformed row")
            replacement[row["id"]] = merged
        self.rows = replacement
        if self.commit_then_error:
            raise TimeoutError("response lost after accepted write")
        return SimpleNamespace(upsert_count=self.count)

    def delete(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("A replacement must not delete the source row first")

    def insert(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("A replacement must use native upsert")

    def flush(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("Acknowledgement does not require a separate flush")


@pytest.fixture
def mutation_store(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, MutationCollection]:
    cls = next(cell.cell_contents for cell in milvus_conn.MilvusConnection.__closure__ or [] if isinstance(cell.cell_contents, type))
    store = object.__new__(cls)
    store._using = "unit-memory-mutation"
    store.logger = logging.getLogger("test.memory.milvus_mutations")
    collection = MutationCollection()
    monkeypatch.setattr(milvus_conn, "Collection", lambda *args, **kwargs: collection)
    monkeypatch.setattr(milvus_conn.utility, "has_collection", lambda *args, **kwargs: True)
    monkeypatch.setattr(milvus_conn, "supports_verified_milvus_3", lambda using: False)
    return store, collection


def test_insert_replaces_complete_row_without_delete_or_insert(mutation_store: tuple[Any, MutationCollection]) -> None:
    store, collection = mutation_store
    value = message(content="replacement", content_embed=[0.0, 1.0], status=False)
    original_input = copy.deepcopy(value)
    assert store.insert([value], "memory_owner", "bank") == []
    assert value == original_input
    assert collection.rows["bank_1"] == store.map_message_to_milvus_fields(value)
    assert len(collection.calls) == 1 and collection.calls[0][1]["partial_update"] is False


@pytest.mark.parametrize("change", [{"id": "foreign_1"}, {"memory_id": "foreign"}, {"message_id": True}, {"content_embed": []}])
def test_insert_preflight_failure_preserves_old_row(mutation_store: tuple[Any, MutationCollection], change: dict[str, Any]) -> None:
    store, collection = mutation_store
    before = copy.deepcopy(collection.rows)
    assert store.insert([message(**change)], "memory_owner", "bank")
    assert collection.rows == before and collection.calls == []


def test_insert_sdk_rejection_preserves_old_row(mutation_store: tuple[Any, MutationCollection]) -> None:
    store, collection = mutation_store
    before = copy.deepcopy(collection.rows)
    collection.write_error = ValueError("server validation failure")
    assert store.insert([message(content="new")], "memory_owner", "bank")
    assert collection.rows == before and len(collection.calls) == 1


@pytest.mark.parametrize("count", [0, 2, None, True, "1"])
def test_insert_mismatched_ack_is_failure_and_never_replayed(mutation_store: tuple[Any, MutationCollection], count: Any) -> None:
    store, collection = mutation_store
    collection.count = count
    assert store.insert([message(content="accepted but unconfirmed")], "memory_owner", "bank")
    assert len(collection.calls) == 1
    assert collection.rows["bank_1"]["content_ltks"] == "accepted but unconfirmed"


def test_insert_unknown_commit_is_not_retried_or_rolled_back(mutation_store: tuple[Any, MutationCollection]) -> None:
    store, collection = mutation_store
    collection.commit_then_error = True
    assert store.insert([message(content="possibly committed")], "memory_owner", "bank")
    assert len(collection.calls) == 1 and collection.rows["bank_1"]["content_ltks"] == "possibly committed"


def test_full_update_preserves_vector_and_extra_metadata(mutation_store: tuple[Any, MutationCollection]) -> None:
    store, collection = mutation_store
    before = copy.deepcopy(collection.rows["bank_1"])
    assert store.update({"message_id": 1}, {"content": "corrected"}, "memory_owner", "bank")
    assert collection.rows["bank_1"] == {**before, "content_ltks": "corrected"}
    assert collection.query_calls[0]["consistency_level"] == "Strong"
    assert collection.query_calls[0]["output_fields"] == ["*"]
    assert collection.calls[0][1]["partial_update"] is False


def test_full_update_rejection_preserves_old_row(mutation_store: tuple[Any, MutationCollection]) -> None:
    store, collection = mutation_store
    before = copy.deepcopy(collection.rows)
    assert store.update({"message_id": 1}, {"content": None}, "memory_owner", "bank") is False
    assert collection.rows == before and len(collection.calls) == 1


def test_verified_status_forget_update_uses_partial_and_preserves_fresh_unrelated_fields(mutation_store: tuple[Any, MutationCollection], monkeypatch: pytest.MonkeyPatch) -> None:
    store, collection = mutation_store
    monkeypatch.setattr(milvus_conn, "supports_verified_milvus_3", lambda using: True)
    collection.before_upsert = lambda rows: rows["bank_1"].update({"extra_meta": {"newer": True}, "q_2_vec": [0.0, 1.0]})
    assert store.update({"message_id": 1}, {"status": False, "forget_at": "2026-11-16 10:00:00"}, "memory_owner", "bank")
    payload, options = collection.calls[0]
    assert payload == [{"id": "bank_1", "status_int": 0, "forget_at": "2026-11-16 10:00:00"}]
    assert options["partial_update"] is True and collection.query_calls[0]["output_fields"] == ["id", "memory_id"]
    assert collection.rows["bank_1"]["extra_meta"] == {"newer": True} and collection.rows["bank_1"]["q_2_vec"] == [0.0, 1.0]


def test_partial_forget_none_clears_storage_string(mutation_store: tuple[Any, MutationCollection], monkeypatch: pytest.MonkeyPatch) -> None:
    store, collection = mutation_store
    monkeypatch.setattr(milvus_conn, "supports_verified_milvus_3", lambda using: True)
    assert store.update({"message_id": 1}, {"forget_at": None}, "memory_owner", "bank")
    assert collection.calls[0][0] == [{"id": "bank_1", "forget_at": ""}]


@pytest.mark.parametrize("native", [False, True])
def test_update_unknown_or_mismatched_ack_is_not_retried(mutation_store: tuple[Any, MutationCollection], monkeypatch: pytest.MonkeyPatch, native: bool) -> None:
    store, collection = mutation_store
    monkeypatch.setattr(milvus_conn, "supports_verified_milvus_3", lambda using: native)
    collection.commit_then_error = True
    assert store.update({"message_id": 1}, {"status": False}, "memory_owner", "bank") is False
    assert len(collection.calls) == 1 and collection.rows["bank_1"]["status_int"] == 0
    collection.commit_then_error = False
    collection.count = 0
    assert store.update({"message_id": 1}, {"status": True}, "memory_owner", "bank") is False
    assert len(collection.calls) == 2 and collection.rows["bank_1"]["status_int"] == 1


@pytest.mark.parametrize("patch", [{"id": "other"}, {"memory_id": "foreign"}, {"message_id": 2}, {"status": "false"}, {"forget_at": True}])
def test_update_invalid_patch_never_writes(mutation_store: tuple[Any, MutationCollection], patch: dict[str, Any]) -> None:
    store, collection = mutation_store
    before = copy.deepcopy(collection.rows)
    assert store.update({"message_id": 1}, patch, "memory_owner", "bank") is False
    assert collection.rows == before and collection.calls == []


@pytest.mark.parametrize("rows", [[], [{"id": "bank_1", "memory_id": "foreign"}], [{"id": "bank_1", "memory_id": "bank"}] * 10000])
def test_missing_foreign_or_incomplete_update_scope_is_not_written(mutation_store: tuple[Any, MutationCollection], rows: list[dict[str, Any]]) -> None:
    store, collection = mutation_store
    collection.query_result = rows
    assert store.update({}, {"status": False}, "memory_owner", "bank") is False
    assert collection.calls == []


class UnknownRpcOutcome(grpc.RpcError):
    def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.UNAVAILABLE

    def details(self) -> str:
        return "unknown commit outcome"


def test_actual_sdk_upsert_handler_does_not_retry_unknown_rpc_with_adapter_options() -> None:
    calls: list[str] = []

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        calls.append("Upsert")
        raise UnknownRpcOutcome()

    handler = SimpleNamespace(_prepare_row_upsert_request=lambda *args, **kwargs: object(), _stub=SimpleNamespace(Upsert=unavailable), _on_rpc_error=lambda error: False)
    with pytest.raises(MilvusException):
        GrpcHandler.upsert_rows(handler, "scratch", [{"id": "bank_1"}], timeout=None, retry_times=0, retry_on_rate_limit=False)
    assert calls == ["Upsert"]


def test_actual_sdk_numeric_timeout_takes_priority_over_zero_retry_count() -> None:
    calls: list[str] = []

    @retry_on_rpc_failure(initial_back_off=0)
    def request(handler: Any, **kwargs: Any) -> str:
        calls.append("Upsert")
        if len(calls) == 1:
            raise UnknownRpcOutcome()
        return "accepted"

    assert request(SimpleNamespace(), timeout=1, retry_times=0, retry_on_rate_limit=False) == "accepted"
    assert calls == ["Upsert", "Upsert"]
