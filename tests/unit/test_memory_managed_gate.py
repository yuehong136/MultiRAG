"""Managed eligibility is durable, shared, and applied before candidate limits."""

from typing import Any

import pytest

from common import settings
from memory.revisions import gate
from memory.revisions.domain import RevisionConflict, content_hash
from memory.services.messages import MessageService


def row(identity: int, source: int = 1, **changes: Any) -> dict[str, Any]:
    return {
        "message_id": identity,
        "memory_id": "bank",
        "message_type": "semantic",
        "source_id": source,
        "user_id": "subject",
        "agent_id": "agent",
        "session_id": "session",
        "content": "fact",
        "status": True,
        "forget_at": None,
        **changes,
    }


def source(state: str = "active", generation: int = 1) -> gate.SourceReadState:
    return gate.SourceReadState(state, 1, generation, content_hash("raw"), {"user_id": "subject", "agent_id": "agent", "session_id": "session"})


def policy(*, state: str = "active", ready: bool = True) -> gate.ManagedReadSnapshot:
    projection = gate.ProjectionReadState("entry", 2, 2, 20, state, content_hash("fact"), ready, ((1, 1, 1, content_hash("raw")), (2, 1, 1, content_hash("raw"))))
    return gate.ManagedReadSnapshot({"bank": ("owner", "active")}, {("bank", 1): source(), ("bank", 2): source()}, {("bank", 20): projection})


def use(monkeypatch: pytest.MonkeyPatch, snapshot: gate.ManagedReadSnapshot) -> None:
    monkeypatch.setattr(gate, "_load_policy", lambda ids, messages: snapshot)


def test_unregistered_legacy_rows_keep_previous_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, policy())
    old = row(99, source=999, status=False)
    assert gate.filter_messages(["owner"], ["bank"], [old]) == [old]


@pytest.mark.parametrize("state,ready", [("inactive", True), ("withdrawn", True), ("active", False)])
def test_unpublished_or_inactive_registered_head_is_not_supplied(monkeypatch: pytest.MonkeyPatch, state: str, ready: bool) -> None:
    use(monkeypatch, policy(state=state, ready=ready))
    assert gate.filter_messages(["owner"], ["bank"], [row(20)]) == []


def test_full_nonanchor_dependency_stops_head_and_late_unknown_derived(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = policy()
    snapshot.sources[("bank", 2)] = source("forgotten", 2)
    use(monkeypatch, snapshot)
    assert gate.filter_messages(["owner"], ["bank"], [row(20), row(99, source=2)]) == []
    exclusions = gate.managed_query_exclusions(["owner"], ["bank"])
    assert exclusions == {"bank": {"message_ids": [2, 20], "source_ids": [2]}}


def test_reenabled_published_source_uses_revision_hash_not_old_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = policy()
    snapshot.sources[("bank", 1)] = source(generation=3)
    use(monkeypatch, snapshot)
    assert gate.filter_messages(["owner"], ["bank"], [row(20)]) == [row(20)]


def test_raw_management_metadata_stays_visible_without_withdrawn_text(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = policy()
    snapshot.sources[("bank", 1)] = source("forgotten", 2)
    use(monkeypatch, snapshot)
    raw = row(1, source=0, message_type="raw", content="raw", extract=[{"content": "private derived"}])
    assert gate.filter_messages(["owner"], ["bank"], [raw]) == []
    metadata = gate.filter_messages(["owner"], ["bank"], [raw], management_metadata=True)[0]
    assert metadata["message_id"] == 1 and metadata["status"] is False and metadata["managed_source_state"] == "forgotten"
    assert "content" not in metadata and "extract" not in metadata
    assert gate.filter_messages(["owner"], ["bank"], [raw], management_body=True) == []


def test_temporary_disabled_raw_has_explicit_management_body_view(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = policy()
    snapshot.sources[("bank", 1)] = source("inactive", 2)
    use(monkeypatch, snapshot)
    raw = row(1, source=0, message_type="raw", content="raw", status=False)
    assert gate.filter_messages(["owner"], ["bank"], [raw]) == []
    assert gate.filter_messages(["owner"], ["bank"], [raw], management_body=True) == [raw]


def test_current_derived_disable_management_view_and_reenable(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, policy(state="inactive"))
    inactive = row(20, status=False)
    assert gate.filter_messages(["owner"], ["bank"], [inactive]) == []
    assert gate.filter_messages(["owner"], ["bank"], [inactive], management_body=True) == [inactive]
    assert gate.filter_messages(["owner"], ["bank"], [row(20)], management_body=True) == [inactive]
    use(monkeypatch, policy())
    assert gate.filter_messages(["owner"], ["bank"], [row(20)]) == [row(20)]


@pytest.mark.parametrize("state,ready,source_state", [("withdrawn", True, "active"), ("inactive", False, "active"), ("inactive", True, "inactive"), ("inactive", True, "forgotten")])
def test_management_body_keeps_permanent_pending_and_inactive_evidence_hidden(monkeypatch: pytest.MonkeyPatch, state: str, ready: bool, source_state: str) -> None:
    snapshot = policy(state=state, ready=ready)
    snapshot.sources[("bank", 2)] = source(source_state)
    use(monkeypatch, snapshot)
    assert gate.filter_messages(["owner"], ["bank"], [row(20)], management_body=True) == []


def test_management_extract_reads_inactive_current_without_normal_preexclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, policy(state="inactive"))
    calls: list[dict[str, Any]] = []

    class Store:
        def search(self, **kwargs: Any) -> Any:
            raise AssertionError("managed management queries must be strict")

        def search_strict(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
            calls.append(kwargs)
            values = [row(1, source=0, message_type="raw", content="raw")] if kwargs["condition"].get("message_type") == "raw" else [row(20, status=False)]
            return values, len(values)

        def get_fields(self, rows: list[dict[str, Any]], fields: list[str]) -> dict[str, dict[str, Any]]:
            return {str(item["message_id"]): {key: value for key, value in item.items() if key in fields} for item in rows}

    monkeypatch.setitem(settings.__dict__, "msgStoreConn", Store())
    result = MessageService.list_message("owner", "bank")
    assert result["message_list"][0]["extract"][0]["message_id"] == 20
    assert result["message_list"][0]["extract"][0]["status"] is False
    assert calls[1]["limit"] == 10000 and "eligibility_by_memory" not in calls[1]


@pytest.mark.parametrize("method", ["read", "write", "query"])
def test_sql_failure_is_not_a_fail_open_unmanaged_snapshot(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    def failed(*args: Any) -> gate.ManagedReadSnapshot:
        raise RuntimeError("SQL state unavailable")

    monkeypatch.setattr(gate, "_load_policy", failed)
    with pytest.raises(RuntimeError, match="SQL state unavailable"):
        if method == "read":
            gate.filter_messages(["owner"], ["bank"], [row(20)])
        elif method == "write":
            gate.assert_legacy_write_allowed("owner", "bank")
        else:
            gate.managed_query_exclusions(["owner"], ["bank"])


def test_managed_writer_and_fifo_are_rejected_before_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, policy())
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", object())
    with pytest.raises(RevisionConflict, match="coordinated"):
        MessageService.insert_message([row(20)], "owner", "bank")
    with pytest.raises(RevisionConflict, match="coordinated"):
        MessageService.pick_messages_to_delete_by_fifo("bank", "owner", 1)


def test_managed_eligibility_is_applied_before_top_n(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = policy()
    snapshot.sources[("bank", 2)] = source("forgotten", 2)
    use(monkeypatch, snapshot)
    calls: list[dict[str, Any]] = []

    class Store:
        def search(self, **kwargs: Any) -> Any:
            raise AssertionError("managed reads cannot use an unfiltered legacy search")

        def search_managed(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
            calls.append(kwargs)
            excluded = kwargs["eligibility_by_memory"]["bank"]["source_ids"]
            rows = [item for item in [row(99, source=2), row(98, source=1)] if item["source_id"] not in excluded][: kwargs["limit"]]
            return rows, len(rows)

        def get_fields(self, rows: list[dict[str, Any]], fields: list[str]) -> dict[str, dict[str, Any]]:
            return {str(item["message_id"]): item for item in rows}

    monkeypatch.setitem(settings.__dict__, "msgStoreConn", Store())
    assert [item["message_id"] for item in MessageService.search_message(["bank"], {}, ["owner"], [], 1)] == [98]
    assert calls[0]["limit"] == 1 and calls[0]["eligibility_by_memory"]["bank"]["source_ids"] == [2]
