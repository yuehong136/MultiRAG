"""The Python CLI operates on registered models through the active REST endpoints."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def cli_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "admin/client"))
    return importlib.import_module("multirag_client")


def test_cli_sets_and_resets_one_registered_model_default(cli_module: Any) -> None:
    model_id = "a" * 32
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(method: str, path: str, **options: Any) -> SimpleNamespace:
        calls.append((method, path, options))
        return SimpleNamespace(status_code=200, json=lambda: {"code": 0, "data": None})

    client = cli_module.MultiRAGClient(SimpleNamespace(request=request), "user")
    client.set_default_model({"model_type": "llm_id", "model_id": model_id})
    client.reset_default_model({"model_type": "img2txt_id"})

    assert calls == [
        ("PATCH", "models/default", {"use_api_base": True, "auth_kind": "web", "json_body": {"model_type": "chat", "model_id": model_id}}),
        ("PATCH", "models/default", {"use_api_base": True, "auth_kind": "web", "json_body": {"model_type": "image2text", "model_id": ""}}),
    ]


def test_cli_lists_registered_ids_and_manages_provider(cli_module: Any) -> None:
    model_properties = {"provider_name": "OpenAI", "instance_name": "default", "effective_status": "active"}
    models = [
        {**model_properties, "id": "a" * 32, "name": "chat", "model_types": ["chat"]},
        {**model_properties, "id": "b" * 32, "name": "embed", "model_types": ["embedding"]},
    ]
    defaults = [{"model_id": models[0]["id"], "model_type": "chat", "model_name": "chat"}]
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(method: str, path: str, **options: Any) -> SimpleNamespace:
        calls.append((method, path, options))
        data = None
        if path == "models":
            data = {"items": [models[options["params"]["page"] - 1]], "total": 101}
        elif path == "models/default":
            data = {"models": defaults}
        return SimpleNamespace(status_code=200, json=lambda: {"code": 0, "data": data})

    client = cli_module.MultiRAGClient(SimpleNamespace(request=request), "user")
    printed: list[Any] = []
    client._print_table_simple = printed.append
    client.create_model_provider({"provider_name": "OpenAI", "provider_key": "fixture-key"})
    client.list_user_model_providers({})
    client.list_user_default_models({})
    client.drop_model_provider({"provider_name": "OpenAI"})

    assert calls == [
        ("POST", "providers/OpenAI/instances", {"use_api_base": True, "auth_kind": "web", "json_body": {"api_key": "fixture-key", "instance_name": "default"}}),
        ("GET", "models", {"use_api_base": True, "auth_kind": "web", "params": {"page": 1, "page_size": 100}}),
        ("GET", "models", {"use_api_base": True, "auth_kind": "web", "params": {"page": 2, "page_size": 100}}),
        ("GET", "models/default", {"use_api_base": True, "auth_kind": "web"}),
        ("DELETE", "providers/OpenAI", {"use_api_base": True, "auth_kind": "web"}),
    ]
    assert [row["model_id"] for row in printed[0]] == [model["id"] for model in models]
    assert [row["model_types"] for row in printed[0]] == ["chat", "embedding"]
    assert printed[1] == defaults
