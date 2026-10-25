"""CLI lists every dataset without hiding errors behind a partial result."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def client(monkeypatch: pytest.MonkeyPatch, replies: list[dict[str, Any]]) -> tuple[Any, list[str], list[Any]]:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "admin/client"))
    module = importlib.import_module("multirag_client")
    paths: list[str] = []
    printed: list[Any] = []

    def request(method: str, path: str, **kwargs: Any) -> Any:
        paths.append(path)
        payload = replies[len(paths) - 1]
        return SimpleNamespace(status_code=200, json=lambda: payload)

    cli = module.MultiRAGClient(SimpleNamespace(request=request), "user")
    monkeypatch.setattr(cli, "_print_table_simple", printed.append)
    return cli, paths, printed


def test_dataset_list_reads_all_pages_and_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"id": str(index)} for index in range(105)]
    cli, paths, printed = client(monkeypatch, [{"code": 0, "data": rows[:100], "total": 105}, {"code": 0, "data": rows[99:], "total": 105}])
    cli.list_user_datasets({})
    assert paths == ["datasets?page=1&page_size=100", "datasets?page=2&page_size=100"]
    assert printed == [rows]


def test_dataset_list_second_page_failure_does_not_print_partial_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cli, _, printed = client(monkeypatch, [{"code": 0, "data": [{"id": "first"}], "total": 101}, {"code": 102, "message": "denied"}])
    cli.list_user_datasets({})
    assert printed == [] and "denied" in capsys.readouterr().out


def test_dataset_list_nonprogress_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    cli, _, printed = client(monkeypatch, [{"code": 0, "data": [{"id": "first"}], "total": 101}, {"code": 0, "data": [{"id": "first"}], "total": 101}])
    with pytest.raises(ValueError, match="stopped before"):
        cli.list_user_datasets({})
    assert printed == []


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 0, "total_datasets": 0},
        {"code": 0, "data": {}, "total_datasets": 0},
        {"code": 0, "data": None},
        {"code": 0, "data": [{"id": "one"}], "total_datasets": 0},
        {"code": 0, "data": [{"id": "one"}], "total_datasets": None},
        {"code": 0, "data": [{"id": "one"}], "total_datasets": 1, "total": 2},
        {"code": 0, "data": [{"id": 1}], "total_datasets": 1},
    ],
)
def test_dataset_list_rejects_missing_inventory_and_inconsistent_totals(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    cli, _, printed = client(monkeypatch, [payload])
    with pytest.raises(ValueError):
        cli.list_user_datasets({})
    assert printed == []


def test_dataset_list_rejects_accumulated_inventory_larger_than_total(monkeypatch: pytest.MonkeyPatch) -> None:
    cli, _, printed = client(monkeypatch, [{"code": 0, "data": [{"id": "one"}], "total_datasets": 2}, {"code": 0, "data": [{"id": "two"}, {"id": "three"}], "total_datasets": 2}])
    with pytest.raises(ValueError, match="Invalid dataset total"):
        cli.list_user_datasets({})
    assert printed == []


def test_dataset_list_accepts_explicit_empty_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    cli, _, printed = client(monkeypatch, [{"code": 0, "data": [], "total_datasets": 0}])
    cli.list_user_datasets({})
    assert printed == [[]]


@pytest.mark.parametrize("tail", [{"code": 0, "data": []}, {"code": 0, "data": [], "total_datasets": 100}])
def test_dataset_list_retains_first_total_across_later_pages(monkeypatch: pytest.MonkeyPatch, tail: dict[str, Any]) -> None:
    rows = [{"id": str(index)} for index in range(100)]
    cli, _, printed = client(monkeypatch, [{"code": 0, "data": rows, "total_datasets": 106}, tail])
    with pytest.raises(ValueError, match=r"stopped before|changed between"):
        cli.list_user_datasets({})
    assert printed == []
