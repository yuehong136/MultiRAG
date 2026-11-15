"""Recent-message pages remain bounded and stable even when the store ignores ordering."""

import base64
import copy
import json
from typing import Any

import pytest

from common import settings
from memory.services.messages import (
    MAX_RECENT_MESSAGES_SCAN,
    MessageService,
    RecentMessageCursorStaleError,
    RecentMessageReadLimitError,
    SourceMessageBindingError,
)


def message(message_id: int, **changes: Any) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "memory_id": "bank",
        "message_type": "raw",
        "source_id": 0,
        "user_id": "subject",
        "agent_id": "agent",
        "session_id": "session",
        "valid_at": "2026-11-15 10:00:00",
        "invalid_at": None,
        "forget_at": None,
        "status": True,
        "content": f"message {message_id}",
        **changes,
    }


class MessageStore:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []
        self.get_calls: list[tuple[str, str, list[str]]] = []
        self.total_override: int | None = None
        self.ignore_conditions = False
        self.get_result: dict[str, Any] | None = None
        self.get_error: Exception | None = None

    def search(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(copy.deepcopy(kwargs))
        rows = [row for row in self.rows if row.get("forget_at") in (None, "", "-")]
        if not self.ignore_conditions:
            for field, value in kwargs["condition"].items():
                if not value:
                    continue
                rows = [row for row in rows if row.get(field) in value] if isinstance(value, list) else [row for row in rows if row.get(field) == value]
        # Deliberately preserve insertion order: Milvus's current filter query ignores order_by.
        return copy.deepcopy(rows[: kwargs["limit"]]), self.total_override if self.total_override is not None else len(rows)

    def get_fields(self, result: list[dict[str, Any]], fields: list[str]) -> dict[str, dict[str, Any]]:
        return {f"row-{index}": {field: row[field] for field in fields if field in row} for index, row in enumerate(result)}

    def get(self, doc_id: str, index_name: str, memory_ids: list[str]) -> dict[str, Any] | None:
        self.get_calls.append((doc_id, index_name, memory_ids.copy()))
        if self.get_error:
            raise self.get_error
        if self.get_result is not None:
            return copy.deepcopy(self.get_result)
        return copy.deepcopy(next((row for row in self.rows if doc_id == f"{row['memory_id']}_{row['message_id']}"), None))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MessageStore:
    value = MessageStore([])
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", value)
    return value


def test_existing_call_keeps_all_types_and_statuses_and_original_order(store: MessageStore) -> None:
    store.rows = [message(2, message_type="semantic", status=False), message(1)]

    rows = MessageService.get_recent_messages(["owner"], ["bank"], "agent", "session", 5)

    assert [row["message_id"] for row in rows] == [2, 1]
    assert store.calls[0]["condition"] == {"agent_id": "agent", "session_id": "session"}
    assert store.calls[0]["order_by"].fields == [("valid_at", 1)]
    assert store.calls[0]["limit"] == 5


def test_optional_filters_preserve_false_and_empty_attribution(store: MessageStore) -> None:
    store.rows = [message(1, user_id="", status=False), message(2), message(3, user_id="", status=False, message_type="semantic")]

    rows = MessageService.get_recent_messages(["owner"], ["bank"], "agent", "session", 5, message_type="raw", user_id="", status=False)

    assert [row["message_id"] for row in rows] == [1]
    assert store.calls[0]["condition"]["status"] == [0]
    assert store.calls[0]["condition"]["user_id"] == [""]


def test_new_page_sorts_numeric_ties_and_ignores_backend_order(store: MessageStore) -> None:
    store.rows = [message(2), message(10), message(1, valid_at="2026-11-16 10:00:00"), message(3)]
    first = MessageService.get_recent_page("owner", "bank", "agent", "session", 2)
    assert [row["message_id"] for row in first["messages"]] == [1, 10]
    assert first["next_cursor"]

    store.rows.reverse()
    second = MessageService.get_recent_page("owner", "bank", "agent", "session", 2, cursor=first["next_cursor"])
    assert [row["message_id"] for row in second["messages"]] == [3, 2]
    assert second["next_cursor"] is None
    assert store.calls[0]["index_names"] == ["memory_owner"]
    assert store.calls[0]["memory_ids"] == ["bank"]
    assert store.calls[0]["offset"] == 0
    assert store.calls[0]["limit"] == MAX_RECENT_MESSAGES_SCAN
    assert store.calls[0]["order_by"].fields == [("valid_at", 1), ("message_id", 1)]


@pytest.mark.parametrize("change", ["content", "insert", "delete", "status"])
def test_cursor_detects_full_collection_changes(store: MessageStore, change: str) -> None:
    store.rows = [message(1), message(2), message(3)]
    first = MessageService.get_recent_page("owner", "bank", "agent", "session", 1)
    if change == "content":
        store.rows[0]["content"] = "corrected source content"
    elif change == "insert":
        store.rows.append(message(4))
    elif change == "delete":
        store.rows.pop(0)
    else:
        store.rows[0]["status"] = False
    with pytest.raises(RecentMessageCursorStaleError):
        MessageService.get_recent_page("owner", "bank", "agent", "session", 1, cursor=first["next_cursor"])


@pytest.mark.parametrize(
    "changed_query",
    [{"uid": "another-owner"}, {"memory_id": "another-bank"}, {"agent_id": "other"}, {"session_id": "other"}, {"limit": 2}, {"status": True}, {"message_type": "raw"}, {"user_id": "other"}],
)
def test_cursor_cannot_be_reused_with_different_scope_or_filters(store: MessageStore, changed_query: dict[str, Any]) -> None:
    store.rows = [message(1), message(2)]
    first = MessageService.get_recent_page("owner", "bank", "agent", "session", 1)
    args = {"uid": "owner", "memory_id": "bank", "agent_id": "agent", "session_id": "session", "limit": 1, "cursor": first["next_cursor"], **changed_query}
    with pytest.raises(ValueError, match="cursor"):
        MessageService.get_recent_page(**args)
    assert len(store.calls) == 1


@pytest.mark.parametrize("cursor", ["", "!", "a" * 513, "e30", "bnVsbA", "W10"])
def test_malformed_cursor_fails_before_read(store: MessageStore, cursor: str) -> None:
    with pytest.raises(ValueError, match="cursor"):
        MessageService.get_recent_page("owner", "bank", "agent", "session", 1, cursor=cursor)
    assert store.calls == []


def test_boolean_cursor_offset_is_not_an_integer_offset(store: MessageStore) -> None:
    store.rows = [message(1), message(2)]
    first = MessageService.get_recent_page("owner", "bank", "agent", "session", 1)
    assert first["next_cursor"] is not None
    encoded = first["next_cursor"]
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    payload["offset"] = True
    altered = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(ValueError, match="cursor"):
        MessageService.get_recent_page("owner", "bank", "agent", "session", 1, cursor=altered)
    assert len(store.calls) == 1


@pytest.mark.parametrize("limit", [0, -1, 101, True])
def test_invalid_page_size_fails_before_read(store: MessageStore, limit: int) -> None:
    with pytest.raises(ValueError, match="Page size"):
        MessageService.get_recent_page("owner", "bank", "agent", "session", limit)
    assert store.calls == []


def test_backend_reported_partial_result_cannot_be_paginated(store: MessageStore) -> None:
    store.rows = [message(1)]
    store.total_override = 2
    with pytest.raises(RecentMessageReadLimitError):
        MessageService.get_recent_page("owner", "bank", "agent", "session", 1)


def test_scan_at_the_cap_cannot_claim_the_candidate_set_is_complete(store: MessageStore) -> None:
    store.rows = [message(index + 1) for index in range(MAX_RECENT_MESSAGES_SCAN)]
    with pytest.raises(RecentMessageReadLimitError):
        MessageService.get_recent_page("owner", "bank", "agent", "session", 100)


def test_page_enforces_exact_selectors_after_a_broad_connector_match(store: MessageStore) -> None:
    store.ignore_conditions = True
    store.rows = [
        message(1, status=False),
        message(2, status=False, session_id="session-suffix"),
        message(3, status=False, user_id="foreign"),
        message(4),
        message(5, status=False, message_type="semantic"),
    ]
    page = MessageService.get_recent_page("owner", "bank", "agent", "session", 100, user_id="subject", status=False, message_type="raw")
    assert [row["message_id"] for row in page["messages"]] == [1]
    assert page["next_cursor"] is None


def test_page_does_not_default_to_raw_or_active_messages(store: MessageStore) -> None:
    store.rows = [message(1, status=False, message_type="semantic"), message(2), message(3, forget_at="2026-11-15 10:00:00")]
    page = MessageService.get_recent_page("owner", "bank", "", "", 100)
    assert [row["message_id"] for row in page["messages"]] == [2, 1]


def test_empty_page_has_no_cursor(store: MessageStore) -> None:
    assert MessageService.get_recent_page("owner", "bank", "", "", 10) == {"messages": [], "next_cursor": None}


@pytest.mark.parametrize("rows", [[message(1, memory_id="foreign")], [message(1), message(1)], [message(True)]])
def test_page_rejects_invalid_or_foreign_identity(store: MessageStore, rows: list[dict[str, Any]]) -> None:
    store.rows = rows
    with pytest.raises(SourceMessageBindingError):
        MessageService.get_recent_page("owner", "bank", "", "", 10)


def test_source_batch_is_scoped_deduplicated_and_omits_missing(store: MessageStore) -> None:
    store.rows = [message(1), message(2, message_type="semantic", source_id=1), message(3, content_embed=[1, 2, 3])]
    sources = MessageService.get_source_messages("owner", "bank", [1, 2, 3, 1, 99])
    assert list(sources) == [1, 2, 3]
    assert sources[2]["message_type"] == "semantic"  # Resolver must report mismatch rather than missing.
    assert "content_embed" not in sources[3]
    assert store.get_calls == [(f"bank_{index}", "memory_owner", ["bank"]) for index in [1, 2, 3, 99]]


@pytest.mark.parametrize("ids", [[0], [-1], [True], ["1"], list(range(1, 102))])
def test_source_batch_validates_ids_and_bound_before_any_read(store: MessageStore, ids: list[Any]) -> None:
    with pytest.raises(ValueError):
        MessageService.get_source_messages("owner", "bank", ids)
    assert store.get_calls == []


@pytest.mark.parametrize("row", [message(1, memory_id="foreign"), message(2), message(True)])
def test_source_batch_rejects_identity_mismatches(store: MessageStore, row: dict[str, Any]) -> None:
    store.get_result = row
    with pytest.raises(SourceMessageBindingError):
        MessageService.get_source_messages("owner", "bank", [1])


def test_source_store_failure_is_not_reported_as_missing(store: MessageStore) -> None:
    store.get_error = RuntimeError("message store unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        MessageService.get_source_messages("owner", "bank", [1])


@pytest.mark.parametrize("filters", [{"message_type": "invalid"}, {"status": 0}])
def test_invalid_new_filters_do_not_reach_store(store: MessageStore, filters: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        MessageService.get_recent_page("owner", "bank", "", "", 10, **filters)
    assert store.calls == []


class NativeMessageStore(MessageStore):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__(rows)
        self.native_calls: list[dict[str, Any]] = []
        self.native_result: list[dict[str, Any]] | None = None
        self.native_error: Exception | None = None

    def supports_recent_message_query(self) -> bool:
        return True

    def query_recent_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.native_calls.append(copy.deepcopy(kwargs))
        if self.native_error:
            raise self.native_error
        if self.native_result is not None:
            return copy.deepcopy(self.native_result)
        rows = [row for row in self.rows if row.get("forget_at") in (None, "", "-")]
        for field, value in kwargs["condition"].items():
            if field in {"agent_id", "session_id"} and not value:
                continue
            rows = [row for row in rows if row.get(field) in value] if isinstance(value, list) else [row for row in rows if row.get(field) == value]
        rows.sort(key=lambda row: (row["valid_at"], row["message_id"]), reverse=True)
        if kwargs["before"] is not None:
            rows = [row for row in rows if (row["valid_at"], row["message_id"]) < kwargs["before"]]
        return copy.deepcopy(rows[: kwargs["limit"]])


@pytest.fixture
def native_store(monkeypatch: pytest.MonkeyPatch) -> NativeMessageStore:
    value = NativeMessageStore([])
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", value)
    return value


def test_capabilities_distinguish_native_live_seek_and_bounded_read(store: MessageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MessageService.get_recent_read_capabilities() == {
        "pagination": "bounded_scan_cursor",
        "max_page_size": 100,
        "max_scan_messages": MAX_RECENT_MESSAGES_SCAN,
        "change_detection": "full_candidate_digest",
        "snapshot_isolation": False,
    }
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", NativeMessageStore([]))
    assert MessageService.get_recent_read_capabilities() == {
        "pagination": "live_seek_cursor",
        "max_page_size": 100,
        "max_scan_messages": None,
        "change_detection": "first_last_boundary",
        "snapshot_isolation": False,
    }


def test_native_pages_seek_numeric_ties_and_recheck_both_boundaries(native_store: NativeMessageStore) -> None:
    native_store.rows = [message(2), message(10), message(1, valid_at="2026-11-16 10:00:00"), message(3)]
    first = MessageService.get_recent_page("owner", "bank", "agent", "session", 2)
    assert [row["message_id"] for row in first["messages"]] == [1, 10]
    native_store.rows.reverse()
    second = MessageService.get_recent_page("owner", "bank", "agent", "session", 2, cursor=first["next_cursor"])
    assert [row["message_id"] for row in second["messages"]] == [3, 2]
    assert second["next_cursor"] is None
    assert native_store.calls == []
    assert native_store.native_calls[0]["limit"] == 3
    assert native_store.native_calls[0]["index_name"] == "memory_owner"
    assert native_store.native_calls[0]["memory_id"] == "bank"
    assert native_store.native_calls[1]["condition"]["message_id"] == [1, 10]
    assert native_store.native_calls[1]["limit"] == 2
    assert native_store.native_calls[2]["before"] == ("2026-11-15 10:00:00", 10)
    assert native_store.native_calls[2]["limit"] == 3


@pytest.mark.parametrize("anchor_id", [10, 9])
@pytest.mark.parametrize("change", ["delete", "content", "forget", "time", "scope", "status"])
def test_native_boundary_changes_require_restart(native_store: NativeMessageStore, anchor_id: int, change: str) -> None:
    native_store.rows = [message(10), message(9), message(8)]
    first = MessageService.get_recent_page("owner", "bank", "agent", "session", 2)
    row = next(row for row in native_store.rows if row["message_id"] == anchor_id)
    if change == "delete":
        native_store.rows.remove(row)
    else:
        key, value = {"content": ("content", "corrected"), "forget": ("forget_at", "2026-11-16"), "time": ("valid_at", "2026-11-14"), "scope": ("session_id", "other"), "status": ("status", False)}[
            change
        ]
        row[key] = value
    with pytest.raises(RecentMessageCursorStaleError, match="boundary"):
        MessageService.get_recent_page("owner", "bank", "agent", "session", 2, cursor=first["next_cursor"])
    assert len(native_store.native_calls) == 2  # No continuation is read after a failed anchor check.


def test_native_cursor_is_live_and_does_not_claim_full_set_change_detection(native_store: NativeMessageStore) -> None:
    native_store.rows = [message(10), message(9), message(7), message(6)]
    first = MessageService.get_recent_page("owner", "bank", "agent", "session", 2)
    native_store.rows.extend([message(11), message(8)])
    native_store.rows[2]["content"] = "changed unvisited candidate"
    second = MessageService.get_recent_page("owner", "bank", "agent", "session", 2, cursor=first["next_cursor"])
    assert [row["message_id"] for row in second["messages"]] == [8, 7]
    assert second["messages"][1]["content"] == "changed unvisited candidate"
    assert 11 not in [row["message_id"] for row in second["messages"]]


def test_native_large_scope_reads_only_page_and_lookahead(native_store: NativeMessageStore) -> None:
    native_store.rows = [message(index) for index in range(1, MAX_RECENT_MESSAGES_SCAN + 2)]
    page = MessageService.get_recent_page("owner", "bank", "", "", 100)
    assert len(page["messages"]) == 100 and page["next_cursor"]
    assert [call["limit"] for call in native_store.native_calls] == [101]
    assert native_store.calls == []


def test_native_empty_read_and_store_error_do_not_fall_back(native_store: NativeMessageStore) -> None:
    assert MessageService.get_recent_page("owner", "bank", "", "", 10) == {"messages": [], "next_cursor": None}
    native_store.native_error = RuntimeError("ordered read failed")
    with pytest.raises(RuntimeError, match="ordered read failed"):
        MessageService.get_recent_page("owner", "bank", "", "", 10)
    assert native_store.calls == []


@pytest.mark.parametrize("change", [{"user_id": "foreign"}, {"limit": 2}, {"status": False}, {"session_id": "foreign"}])
def test_native_cursor_binds_query_before_any_message_read(native_store: NativeMessageStore, change: dict[str, Any]) -> None:
    native_store.rows = [message(3), message(2), message(1)]
    first = MessageService.get_recent_page("owner", "bank", "agent", "session", 1)
    options = {"uid": "owner", "memory_id": "bank", "agent_id": "agent", "session_id": "session", "limit": 1, **change, "cursor": first["next_cursor"]}
    with pytest.raises(ValueError, match="cursor"):
        MessageService.get_recent_page(**options)
    assert len(native_store.native_calls) == 1


@pytest.mark.parametrize(
    "rows",
    [
        [message(1, memory_id="foreign")],
        [message(1, user_id="foreign")],
        [message(1, forget_at="2026-11-16")],
        [message(1), message(2)],
        [message(1), message(1)],
        [message(1, valid_at=None)],
    ],
)
def test_native_rejects_foreign_unordered_or_malformed_results(native_store: NativeMessageStore, rows: list[dict[str, Any]]) -> None:
    native_store.native_result = rows
    with pytest.raises(SourceMessageBindingError):
        MessageService.get_recent_page("owner", "bank", "agent", "session", 1, user_id="subject")


@pytest.mark.parametrize("malformation", ["bool_id", "bool_version", "wrong_mode", "invalid_digest", "inverted_keys"])
def test_native_cursor_rejects_malformed_anchor_before_read(native_store: NativeMessageStore, malformation: str) -> None:
    native_store.rows = [message(3), message(2), message(1)]
    first = MessageService.get_recent_page("owner", "bank", "agent", "session", 1)
    encoded = first["next_cursor"]
    assert encoded is not None
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    if malformation == "bool_id":
        payload["last"]["id"] = True
    elif malformation == "bool_version":
        payload["v"] = True
    elif malformation == "wrong_mode":
        payload["v"] = 1
    elif malformation == "invalid_digest":
        payload["first"]["digest"] = "x" * 64
    else:
        payload["last"]["id"] = 9
    altered = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(ValueError, match="cursor"):
        MessageService.get_recent_page("owner", "bank", "agent", "session", 1, cursor=altered)
    assert len(native_store.native_calls) == 1


@pytest.fixture
def milvus_recent_store(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, list[dict[str, Any]]]:
    from types import SimpleNamespace

    from memory.utils import milvus_conn

    cls = next(cell.cell_contents for cell in milvus_conn.MilvusConnection.__closure__ or [] if isinstance(cell.cell_contents, type))
    store = object.__new__(cls)
    store._using = "unit-recent"
    calls: list[dict[str, Any]] = []

    def query(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        return [{"id": "bank_1", "message_id": 1}]

    collection = SimpleNamespace(load=lambda: None, query=query)
    monkeypatch.setattr(milvus_conn, "Collection", lambda name, using: collection)
    monkeypatch.setattr(milvus_conn.utility, "has_collection", lambda name, using: True)
    return store, milvus_conn, calls


@pytest.mark.parametrize(
    "sdk,server,expected",
    [
        ("2.5.11", "3.0.6", False),
        ("3.0.3", "2.6.22", False),
        ("3.0.3", "3.0.6", True),
        ("3.0.3", "v3.0.6", True),
        ("3.0.3", "3.0-20261110-63100f8f5b4", True),
        ("3.0.3", "3.0.0-beta", False),
        ("3.0.3", "3.0.6garbage", False),
        ("3.0.3", "3.0-20261110-unknown", False),
        ("3.0.3rc1", "3.0.6", False),
        ("3.0.3", "unknown", False),
    ],
)
def test_native_capability_checks_both_sdk_and_actual_server(
    milvus_recent_store: tuple[Any, Any, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch, sdk: str, server: str, expected: bool
) -> None:
    store, module, calls = milvus_recent_store
    version_calls: list[dict[str, Any]] = []
    from common.doc_store import milvus_capabilities

    monkeypatch.setattr(milvus_capabilities, "version", lambda name: sdk)

    def get_version(**kwargs: Any) -> str:
        version_calls.append(kwargs)
        return server

    monkeypatch.setattr(module.utility, "get_server_version", get_version)
    assert store.supports_recent_message_query() is expected
    assert version_calls == ([] if sdk in {"2.5.11", "3.0.3rc1"} else [{"using": "unit-recent", "timeout": 5}])
    assert calls == []


def test_native_capability_does_not_hide_server_failure(milvus_recent_store: tuple[Any, Any, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch) -> None:
    store, module, _ = milvus_recent_store
    from common.doc_store import milvus_capabilities

    monkeypatch.setattr(milvus_capabilities, "version", lambda name: "3.0.3")

    def failed_version(**kwargs: Any) -> str:
        raise RuntimeError("unavailable")

    monkeypatch.setattr(module.utility, "get_server_version", failed_version)
    with pytest.raises(RuntimeError, match="unavailable"):
        store.supports_recent_message_query()


def test_native_milvus_query_uses_safe_scope_range_order_and_strong_read(milvus_recent_store: tuple[Any, Any, list[dict[str, Any]]]) -> None:
    store, _, calls = milvus_recent_store
    selected = ["message_id", "valid_at", "status", "content"]
    result = store.query_recent_rows(selected, {"session_id": ['s" or true'], "user_id": [""], "status": [0]}, "memory_owner", 'bank" or true', 11, before=('2026-11-15" or true', 17))
    assert result == [{"id": "bank_1", "message_id": 1}]
    assert len(calls) == 1
    request = calls[0]
    assert request["order_by"] == ["valid_at:desc", "message_id:desc"]
    assert request["consistency_level"] == "Strong" and request["limit"] == 11
    assert request["output_fields"] == ["message_id", "valid_at", "status_int", "content_ltks", "id"]
    assert "offset" not in request
    assert 'memory_id == "bank\\" or true"' in request["expr"]
    assert 'session_id in ["s\\" or true"]' in request["expr"]
    assert 'user_id in [""]' in request["expr"] and "status_int in [0]" in request["expr"]
    assert 'valid_at < "2026-11-15\\" or true"' in request["expr"]
    assert "message_id < 17" in request["expr"] and 'forget_at == ""' in request["expr"]


def test_native_milvus_failure_is_not_an_empty_success(milvus_recent_store: tuple[Any, Any, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    store, module, _ = milvus_recent_store

    def failed_query(**kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("native scalar order rejected")

    monkeypatch.setattr(module, "Collection", lambda name, using: SimpleNamespace(load=lambda: None, query=failed_query))
    with pytest.raises(RuntimeError, match="native scalar order rejected"):
        store.query_recent_rows(["message_id"], {}, "memory_owner", "bank", 2)


@pytest.mark.parametrize("limit,before", [(0, None), (102, None), (True, None), (2, ("time", True)), (2, ("time", -1))])
def test_native_milvus_rejects_invalid_bounds_before_query(milvus_recent_store: tuple[Any, Any, list[dict[str, Any]]], limit: int, before: tuple[str, int] | None) -> None:
    store, _, calls = milvus_recent_store
    with pytest.raises(ValueError):
        store.query_recent_rows(["message_id"], {}, "memory_owner", "bank", limit, before=before)
    assert calls == []


def test_native_rejects_rows_not_below_seek_boundary(native_store: NativeMessageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    native_store.rows = [message(3), message(2), message(1)]
    first = MessageService.get_recent_page("owner", "bank", "agent", "session", 1)
    original = native_store.query_recent_rows

    def broken_range(**kwargs: Any) -> list[dict[str, Any]]:
        if kwargs["before"] is not None:
            return [message(3)]
        return original(**kwargs)

    monkeypatch.setattr(native_store, "query_recent_rows", broken_range)
    with pytest.raises(SourceMessageBindingError, match="outside"):
        MessageService.get_recent_page("owner", "bank", "agent", "session", 1, cursor=first["next_cursor"])


class StrictSourceMessageStore(MessageStore):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__(rows)
        self.source_calls: list[dict[str, Any]] = []
        self.source_error: Exception | None = None
        self.source_result: list[dict[str, Any]] | None = None

    def query_source_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.source_calls.append(copy.deepcopy(kwargs))
        if self.source_error is not None:
            raise self.source_error
        if self.source_result is not None:
            return copy.deepcopy(self.source_result)
        return copy.deepcopy([row for row in self.rows if row["message_id"] in kwargs["message_ids"]])


def test_strict_source_batch_keeps_inactive_forgotten_and_nonraw_for_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    store = StrictSourceMessageStore([message(1, status=False, forget_at="2026-11-16", content_embed=[1]), message(2, message_type="semantic", source_id=1)])
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", store)
    result = MessageService.get_source_messages("owner", "bank", [1, 2, 99, 1])
    assert set(result) == {1, 2}
    assert result[1]["status"] is False and result[1]["forget_at"] == "2026-11-16"
    assert result[2]["message_type"] == "semantic" and result[2]["source_id"] == 1
    assert "content_embed" not in result[1]
    assert store.source_calls[0]["message_ids"] == [1, 2, 99]
    assert store.source_calls[0]["index_name"] == "memory_owner" and store.source_calls[0]["memory_id"] == "bank"
    assert store.get_calls == []


def test_strict_source_store_failure_cannot_be_marked_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    store = StrictSourceMessageStore([])
    store.source_error = RuntimeError("source batch unavailable")
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", store)
    with pytest.raises(RuntimeError, match="source batch unavailable"):
        MessageService.get_source_messages("owner", "bank", [1])
    assert store.get_calls == []


@pytest.mark.parametrize("result", [[message(1, memory_id="foreign")], [message(2)], [message(1), message(1)]])
def test_strict_source_batch_rejects_foreign_unrequested_and_duplicate_rows(monkeypatch: pytest.MonkeyPatch, result: list[dict[str, Any]]) -> None:
    store = StrictSourceMessageStore([])
    store.source_result = result
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", store)
    with pytest.raises(SourceMessageBindingError):
        MessageService.get_source_messages("owner", "bank", [1])


def test_strict_milvus_source_batch_uses_exact_ids_and_keeps_lifecycle_state(milvus_recent_store: tuple[Any, Any, list[dict[str, Any]]]) -> None:
    store, _, calls = milvus_recent_store
    result = store.query_source_rows(["message_id", "status", "forget_at", "content"], [1, 2, 1], "memory_owner", 'bank" or true')
    assert result == [{"id": "bank_1", "message_id": 1}]
    assert calls[0]["expr"] == 'memory_id == "bank\\" or true" and message_id in [1, 2]'
    assert "forget_at" not in calls[0]["expr"] and "status_int" not in calls[0]["expr"]
    assert calls[0]["consistency_level"] == "Strong" and calls[0]["limit"] == 2


def test_strict_milvus_source_error_propagates(milvus_recent_store: tuple[Any, Any, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    store, module, _ = milvus_recent_store

    def unavailable(**kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("source query failed")

    monkeypatch.setattr(module, "Collection", lambda name, using: SimpleNamespace(load=lambda: None, query=unavailable))
    with pytest.raises(RuntimeError, match="source query failed"):
        store.query_source_rows(["message_id"], [1], "memory_owner", "bank")


class StrictCandidateMessageStore(MessageStore):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__(rows)
        self.candidate_calls: list[dict[str, Any]] = []
        self.candidate_error: Exception | None = None

    def query_recent_candidates(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.candidate_calls.append(copy.deepcopy(kwargs))
        if self.candidate_error is not None:
            raise self.candidate_error
        return copy.deepcopy(self.rows[: kwargs["limit"]])


def test_bounded_fallback_uses_strict_milvus_candidates_and_sorts(monkeypatch: pytest.MonkeyPatch) -> None:
    store = StrictCandidateMessageStore([message(2), message(10), message(1)])
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", store)
    result = MessageService.get_recent_page("owner", "bank", "agent", "session", 2)
    assert [row["message_id"] for row in result["messages"]] == [10, 2]
    assert store.calls == []
    assert store.candidate_calls[0]["limit"] == MAX_RECENT_MESSAGES_SCAN
    assert store.candidate_calls[0]["index_name"] == "memory_owner" and store.candidate_calls[0]["memory_id"] == "bank"


def test_bounded_fallback_storage_failure_cannot_be_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    store = StrictCandidateMessageStore([])
    store.candidate_error = RuntimeError("bounded query failed")
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", store)
    with pytest.raises(RuntimeError, match="bounded query failed"):
        MessageService.get_recent_page("owner", "bank", "", "", 10)
    assert store.calls == []


def test_bounded_fallback_strict_read_still_rejects_scan_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    store = StrictCandidateMessageStore([message(index) for index in range(1, MAX_RECENT_MESSAGES_SCAN + 1)])
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", store)
    with pytest.raises(RecentMessageReadLimitError):
        MessageService.get_recent_page("owner", "bank", "", "", 10)


def test_strict_milvus_bounded_candidates_use_strong_filter_without_unsupported_order(milvus_recent_store: tuple[Any, Any, list[dict[str, Any]]]) -> None:
    store, _, calls = milvus_recent_store
    store.query_recent_candidates(["message_id", "content"], {"session_id": ["session"], "status": [0]}, "memory_owner", "bank", MAX_RECENT_MESSAGES_SCAN)
    assert calls[0]["consistency_level"] == "Strong" and calls[0]["limit"] == MAX_RECENT_MESSAGES_SCAN
    assert 'memory_id == "bank"' in calls[0]["expr"] and 'session_id in ["session"]' in calls[0]["expr"]
    assert "status_int in [0]" in calls[0]["expr"] and 'forget_at == ""' in calls[0]["expr"]
    assert "order_by" not in calls[0] and "offset" not in calls[0]


def test_strict_milvus_bounded_error_propagates(milvus_recent_store: tuple[Any, Any, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    store, module, _ = milvus_recent_store

    def unavailable(**kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("bounded query failed")

    monkeypatch.setattr(module, "Collection", lambda name, using: SimpleNamespace(load=lambda: None, query=unavailable))
    with pytest.raises(RuntimeError, match="bounded query failed"):
        store.query_recent_candidates(["message_id"], {}, "memory_owner", "bank", MAX_RECENT_MESSAGES_SCAN)


@pytest.fixture
def failing_milvus_search(milvus_recent_store: tuple[Any, Any, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch) -> Any:
    import logging
    from types import SimpleNamespace

    from common.doc_store.doc_store_base import MatchDenseExpr

    store, module, _ = milvus_recent_store
    store.logger = logging.getLogger("test.memory.strict_search")

    def unavailable(**kwargs: Any) -> list[Any]:
        raise RuntimeError("private vector storage failure")

    monkeypatch.setattr(module, "Collection", lambda name, using: SimpleNamespace(load=lambda: None, search=unavailable))
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", store)
    return MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine")


def test_milvus_search_keeps_legacy_empty_result_but_strict_chain_propagates(failing_milvus_search: Any) -> None:
    assert MessageService.search_message(["bank"], {}, ["owner"], [failing_milvus_search], 5) == []
    with pytest.raises(RuntimeError, match="private vector storage failure"):
        MessageService.search_message(["bank"], {}, ["owner"], [failing_milvus_search], 5, strict=True)


def test_strict_search_uses_ordinary_propagating_connector_when_no_hook(store: MessageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(**kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        raise RuntimeError("connector unavailable")

    monkeypatch.setattr(store, "search", unavailable)
    with pytest.raises(RuntimeError, match="connector unavailable"):
        MessageService.search_message(["bank"], {}, ["owner"], [], 5, strict=True)


def test_new_context_api_reports_real_strict_milvus_failure_as_500(client: Any, failing_milvus_search: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_context_service
    from common.constants import RetCode

    async def retrieve(*args: Any, **kwargs: Any) -> dict[str, Any]:
        messages = MessageService.search_message(["bank"], {}, ["owner"], [failing_milvus_search], 5, strict=True)
        return {"items": messages}

    monkeypatch.setattr(memory_context_service, "query_context", retrieve)
    response = client.post("/api/v1/memories/bank/context", json={"query": "decision"})
    assert response.status_code == 500
    assert response.json()["retcode"] == int(RetCode.SERVER_ERROR)
    assert response.json()["data"] is False
    assert "private vector storage failure" not in response.text


def test_ob_search_sql_failure_already_propagates_through_strict_service(monkeypatch: pytest.MonkeyPatch) -> None:
    import logging
    from types import SimpleNamespace

    from common.doc_store.doc_store_base import MatchDenseExpr
    from memory.utils.ob_conn import OBConnection

    cls = next(cell.cell_contents for cell in OBConnection.__closure__ or [] if isinstance(cell.cell_contents, type))
    store = object.__new__(cls)
    store.logger = logging.getLogger("test.memory.ob_search")
    store._check_table_exists_cached = lambda table: True
    store._get_filters = lambda condition: []

    def unavailable(sql: str) -> Any:
        raise RuntimeError("controlled OB SQL failure")

    store.client = SimpleNamespace(perform_raw_text_sql=unavailable)
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", store)
    with pytest.raises(RuntimeError, match="controlled OB SQL failure"):
        MessageService.search_message(["bank"], {}, ["owner"], [MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine")], 5, strict=True)
