"""The active Python CLI must not call the retired chunk retrieval route."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def test_cli_uses_rest_search_and_complete_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "admin/client"))
    module = importlib.import_module("multirag_client")
    client = object.__new__(module.MultiRAGClient)
    client.server_type = "user"
    client._get_dataset_id = lambda name: {"one": "a/b", "two": "second"}[name]
    calls = []
    result = {"code": 0, "data": {"chunks": [{"text": "result"}]}}

    def request(*args: Any, **kwargs: Any) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: result)

    client.http_client = SimpleNamespace(request=request)
    printed = []
    client._print_table_simple = printed.append
    client.search_on_datasets({"datasets": ["one", "two"], "question": "q"})
    args, options = calls[0]
    assert args == ("POST", "datasets/a%2Fb/search")
    assert options["use_api_base"] is True and options["auth_kind"] == "web"
    assert options["json_body"] == {"question": "q", "dataset_ids": ["a/b", "second"], "similarity_threshold": 0.2, "vector_similarity_weight": 0.3}
    assert printed == [[{"text": "result"}]]
