"""Tool inputs resolve fragments while whole references keep their value type."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent.canvas import Canvas
from agent.component.base import ComponentBase
from agent.tools import email


def _component(value: Any) -> ComponentBase:
    canvas = Canvas(json.dumps({"components": {}, "path": [], "retrieval": [], "variables": {}}))
    canvas.globals.update({"sys.query": "Ada", "env.data": {"items": [1, 2]}, "env.empty": None, "env.flag": False, "env.zero": 0})
    source = SimpleNamespace(output=lambda key: {"name": "Ada", "_references": None}.get(key))
    canvas.components["UserFillUp:X"] = {"obj": source, "name": "Input"}
    canvas.components["userfillup_test"] = {"obj": source, "name": "Input"}
    component = ComponentBase.__new__(ComponentBase)
    component._canvas = canvas
    component._param = SimpleNamespace(inputs={"subject": {}}, subject=value)
    return component


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("{sys.query}@example.test", "Ada@example.test"),
        ("{UserFillUp:X@name}@example.test", "Ada@example.test"),
        ("{userfillup_test@name}@example.test", "Ada@example.test"),
        (" {sys.query} ", " Ada "),
        ("{UserFillUp:X@name} ", "Ada "),
        ("Question from {UserFillUp:X@name}: {sys.query}", "Question from Ada: Ada"),
        ("{env.flag}/{env.zero}/{env.empty}", "False/0/"),
        ("{sys.query}/{sys.query}", "Ada/Ada"),
        ("literal {unknown} 1 + 2", "literal {unknown} 1 + 2"),
        ("{env.data}", {"items": [1, 2]}),
        ("{env.flag}", False),
        ("env.zero", 0),
        ("{env.empty}", None),
        ({"nested": "{sys.query}"}, {"nested": "{sys.query}"}),
        (["{sys.query}"], ["{sys.query}"]),
        (7, 7),
        (False, False),
    ],
)
def test_input_template_values(value: Any, expected: Any) -> None:
    component = _component(value)
    result = component.get_input()
    assert result == {"subject": expected}
    assert type(result["subject"]) is type(expected)
    assert component.get_input("subject") == expected


def test_missing_global_and_component_keep_error_contract() -> None:
    for value in ("prefix {env.missing}", "prefix {Missing:X@name}"):
        component = _component(value)
        with pytest.raises((KeyError, Exception), match=r"env.missing|Can't find variable"):
            component.get_input()


def test_none_parameter_is_still_omitted() -> None:
    assert _component(None).get_input() == {}


def test_email_consumes_resolved_canvas_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    canvas = _component("")._canvas
    param = email.EmailParam()
    param.smtp_server, param.email, param.password, param.sender_name = "smtp.invalid", "sender@example.test", "unused", "Test"
    param.to_email = "{UserFillUp:X@name}@example.test"
    param.subject = "Question from {sys.query}"
    param.content = "Literal content"
    tool = email.Email(canvas, "Email:test", param)
    smtp = MagicMock()
    monkeypatch.setattr(email.smtplib, "SMTP", smtp)
    assert tool.invoke(**tool.get_input()) is True
    sent = smtp.return_value.__enter__.return_value.send_message.call_args
    assert sent.args[0]["To"] == "Ada@example.test"
    assert sent.args[2] == ["Ada@example.test"]
