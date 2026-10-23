"""Admin variable parsing and fail-closed CLI acknowledgements."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lark import Lark


@pytest.fixture
def cli_modules(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "admin/client"))
    return importlib.import_module("parser"), importlib.import_module("multirag_client")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0", "0"), ("false", "false"), ("-1", "-1"), ('"local host"', "local host"), ("''", ""), ("'{\"enabled\":false}'", '{"enabled":false}'), ("\"'edge'\"", "'edge'")],
)
def test_variable_parser_preserves_value(cli_modules: tuple[Any, Any], text: str, expected: str) -> None:
    parser_module, module = cli_modules
    parser = Lark(parser_module.GRAMMAR, start="start", parser="lalr", transformer=parser_module.MultiRAGCLITransformer())
    command = parser.parse(f"SET VAR mail.value {text};").children[0]
    assert module._variable_text(command["var_value"]) == expected
    calls: list[dict[str, Any]] = []

    def request(*args: Any, **kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, json=lambda: {"code": 0, "message": "Set variable successfully"})

    client = object.__new__(module.MultiRAGClient)
    client.server_type = "admin"
    client.http_client = SimpleNamespace(request=request)
    module.run_command(client, command)
    assert calls[0]["json_body"] == {"var_name": "mail.value", "var_value": expected}


@pytest.mark.parametrize("body", [{}, {"code": False}, {"code": "0"}, {"code": 400}, {"code": 0, "retcode": 1}])
def test_variable_cli_rejects_unacknowledged_result(cli_modules: tuple[Any, Any], body: dict[str, Any]) -> None:
    _, module = cli_modules
    with pytest.raises(RuntimeError):
        module._variable_response(SimpleNamespace(status_code=200, json=lambda: body))


def test_variable_cli_mode_and_get_query(cli_modules: tuple[Any, Any]) -> None:
    _, module = cli_modules
    client = object.__new__(module.MultiRAGClient)
    client.server_type = "user"
    with pytest.raises(RuntimeError, match="ADMIN"):
        client.set_variable({"var_name": "mail.server", "var_value": ""})
    calls: list[dict[str, Any]] = []

    def request(*args: Any, **kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, json=lambda: {"code": 0, "data": []})

    client.server_type = "admin"
    client.http_client = SimpleNamespace(request=request)
    client._print_table_simple = lambda rows: None
    client.show_variable({"var_name": "mail"})
    assert calls == [{"params": {"var_name": "mail"}, "use_api_base": True, "auth_kind": "admin"}]


@pytest.mark.parametrize("data", [None, "invalid", {}, [False], ["invalid"]])
def test_variable_cli_rejects_malformed_rows(cli_modules: tuple[Any, Any], data: Any) -> None:
    _, module = cli_modules
    with pytest.raises(RuntimeError, match="Invalid variable list"):
        module._variable_rows(SimpleNamespace(status_code=200, json=lambda: {"code": 0, "data": data}))
