"""The real CLI consumes complete REST inventories and reports parse outcomes."""

import importlib
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def _client(monkeypatch: pytest.MonkeyPatch, replies: list[dict[str, Any]]) -> tuple[ModuleType, Any, list[dict[str, Any]]]:
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "admin/client"))
    module = importlib.import_module("multirag_client")
    assert Path(module.__file__).resolve() == root / "admin/client/multirag_client.py"
    calls: list[dict[str, Any]] = []

    def request(method: str, path: str, **kwargs: Any) -> Any:
        calls.append(deepcopy({"method": method, "path": path, **kwargs}))
        reply = replies[len(calls) - 1]
        return SimpleNamespace(status_code=reply.get("status", 200), json=lambda: reply["body"])

    return module, module.MultiRAGClient(SimpleNamespace(request=request), "user"), calls


def _documents(rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
    return {"body": {"code": 0, "data": {"docs": rows, "total": total}}}


def test_parse_poll_reads_every_target_across_pages_and_observes_state_change(monkeypatch: pytest.MonkeyPatch) -> None:
    running = [{"id": f"document-{index:03d}", "run": "RUNNING"} for index in range(105)]
    done = [{**row, "run": "DONE"} for row in running]
    module, cli, calls = _client(monkeypatch, [_documents(rows, 105) for rows in (running[:100], running[100:], done[:100], done[100:])])
    sleeps: list[float] = []
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 0, sleep=sleeps.append))
    identifiers = [row["id"] for row in running]
    original = list(identifiers)
    assert cli._wait_parse_done("owned", "dataset", identifiers)
    assert identifiers == original and sleeps == [0.5]
    assert [call["params"]["page"] for call in calls] == [1, 2, 1, 2]
    assert all(call["params"] == {"page": page, "page_size": 100, "orderby": "id", "desc": False, "ids": identifiers} for call, page in zip(calls, [1, 2, 1, 2], strict=True))
    assert all(call["use_api_base"] and call["auth_kind"] == "web" for call in calls)


def test_empty_parse_selection_is_not_success_and_has_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _, cli, calls = _client(monkeypatch, [])
    assert not cli._wait_parse_done("owned", "dataset", [])
    assert calls == []


@pytest.mark.parametrize("rows", [[], [{"id": "different", "run": "DONE"}], [{"id": "target", "run": "UNKNOWN"}], [{"id": "target"}], [{"id": "target", "run": []}]])
def test_missing_or_invalid_target_status_cannot_pass(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], rows: list[dict[str, Any]]) -> None:
    _, cli, _ = _client(monkeypatch, [_documents(rows, len(rows))])
    assert not cli._wait_parse_done("owned", "dataset", ["target"])
    assert "Parsing status is unavailable" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["FAIL", "CANCEL"])
def test_terminal_parse_status_is_diagnosed_without_sleep_or_provider_payload(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: str) -> None:
    module, cli, _ = _client(monkeypatch, [_documents([{"id": "target", "run": status, "progress_msg": "secret provider payload"}], 1)])
    sleeps: list[float] = []
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 0, sleep=sleeps.append))
    assert not cli._wait_parse_done("owned", "dataset", ["target"])
    output = capsys.readouterr().out
    assert f"Parsing stopped for dataset owned: {status}=1" in output
    assert "secret" not in output and sleeps == []


def test_parse_timeout_reports_last_confirmed_status_counts(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rows = [{"id": "first", "run": "RUNNING"}, {"id": "second", "run": "SCHEDULE"}, {"id": "third", "run": "DONE"}]
    module, cli, _ = _client(monkeypatch, [_documents(rows, 3)])
    ticks = iter([0, 61])
    sleeps: list[float] = []
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=sleeps.append))
    assert not cli._wait_parse_done("owned", "dataset", [row["id"] for row in rows])
    assert "Parsing timed out after 60s for dataset owned: DONE=1, RUNNING=1, SCHEDULE=1" in capsys.readouterr().out
    assert sleeps == []


@pytest.mark.parametrize(
    "body",
    [
        {"code": 102, "message": "secret response"},
        {"code": True, "data": {"docs": [], "total": 0}},
        {"code": 0, "data": {}},
        {"code": 0, "data": {"docs": [], "total": 1}},
        {"code": 0, "data": {"docs": [{"id": "target"}], "total": 0}},
    ],
)
def test_document_poll_rejects_unconfirmed_or_incomplete_inventory(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], body: dict[str, Any]) -> None:
    _, cli, _ = _client(monkeypatch, [{"body": body}])
    assert not cli._wait_parse_done("owned", "dataset", ["target"])
    output = capsys.readouterr().out
    assert "Fail to list documents" in output and "secret" not in output


def test_document_poll_rejects_repeated_page_without_partial_success(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"id": f"document-{index:03d}", "run": "DONE"} for index in range(100)]
    _, cli, calls = _client(monkeypatch, [_documents(rows, 101), _documents(rows, 101)])
    assert not cli._wait_parse_done("owned", "dataset", [*[row["id"] for row in rows], "last"])
    assert len(calls) == 2


@pytest.mark.parametrize("count", [0, 100, 205])
def test_session_inventory_reads_all_bounded_stably_ordered_pages(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    rows = [{"id": f"session-{index:03d}"} for index in range(count)]
    replies = [{"body": {"code": 0, "data": rows[start : start + 100]}} for start in range(0, count + 1, 100)]
    _, cli, calls = _client(monkeypatch, replies)
    assert cli._list_chat_sessions("chat") == rows
    assert [call["params"]["page"] for call in calls] == list(range(1, len(replies) + 1))
    assert all(call["path"] == "chats/chat/sessions" and call["params"]["page_size"] == 100 and call["params"]["orderby"] == "id" and call["params"]["desc"] is False for call in calls)


@pytest.mark.parametrize(
    "tail",
    [
        {"code": 109, "message": "secret server payload"},
        {"code": 0, "data": [{"id": "session-000"}]},
        {"code": 0, "data": [{"id": 0}]},
        {"code": 0, "data": None},
    ],
)
def test_session_second_page_errors_reject_partial_inventory(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tail: dict[str, Any]) -> None:
    rows = [{"id": f"session-{index:03d}"} for index in range(100)]
    _, cli, calls = _client(monkeypatch, [{"body": {"code": 0, "data": rows}}, {"body": tail}])
    assert cli._list_chat_sessions("chat") is None
    assert len(calls) == 2
    output = capsys.readouterr().out
    assert "Fail to list chat sessions" in output and "secret" not in output


def test_public_drop_session_finds_target_beyond_first_page(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sessions = [{"id": f"session-{index:03d}"} for index in range(101)]
    replies = [
        {"body": {"code": 0, "data": {"chats": [{"id": "chat", "name": "owned"}]}}},
        {"body": {"code": 0, "data": sessions[:100]}},
        {"body": {"code": 0, "data": sessions[100:]}},
        {"body": {"code": 0, "data": True}},
    ]
    _, cli, calls = _client(monkeypatch, replies)
    cli.drop_chat_session({"chat_name": "owned", "session_id": "session-100"})
    assert calls[-1]["method"] == "DELETE" and calls[-1]["json_body"] == {"ids": ["session-100"]}
    assert "Success to drop chat session 'session-100'" in capsys.readouterr().out


def test_public_drop_session_does_not_mutate_after_second_page_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    replies = [
        {"body": {"code": 0, "data": {"chats": [{"id": "chat", "name": "owned"}]}}},
        {"body": {"code": 0, "data": [{"id": f"session-{index:03d}"} for index in range(100)]}},
        {"status": 503, "body": {"code": 500, "message": "temporary failure"}},
    ]
    _, cli, calls = _client(monkeypatch, replies)
    cli.drop_chat_session({"chat_name": "owned", "session_id": "session-001"})
    assert all(call["method"] == "GET" for call in calls)
    assert "Success to drop" not in capsys.readouterr().out
