import copy
import json
import logging
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent.component.docs_generator import DocGeneratorParam
from agent.component.message import Message, MessageParam


def _download(include: bool) -> dict[str, Any]:
    return {
        "doc_id": "document-1",
        "filename": r"中文 C:\1\new\report.pdf",
        "mime_type": "application/pdf",
        "size": 3,
        "base64": "AP/+",
        "preview_url": "/protected/preview/document-1",
        "extra": {"provider": "custom", "count": 0},
        "include_download_info_in_content": include,
    }


@pytest.mark.parametrize("include", [False, True])
@pytest.mark.parametrize("shape", ["dict", "json", "list", "json-list"])
def test_download_metadata_survives_stringify_without_mutating_input(include: bool, shape: str) -> None:
    item = _download(include)
    value: Any = [item] if "list" in shape else item
    if "json" in shape:
        value = json.dumps(value)
    before = copy.deepcopy(value)
    downloads: list[dict[str, Any]] = []
    message = object.__new__(Message)
    result = message._stringify_message_value(value, downloads=downloads)
    expected = {k: v for k, v in item.items() if k != "include_download_info_in_content"}
    assert downloads == [expected] and value == before
    assert json.loads(result) == ([expected] if "list" in shape else expected) if include else result == ""


@pytest.mark.parametrize("include", [False, True])
@pytest.mark.parametrize("mode", ["stream", "plain", "jinja"])
async def test_selected_message_template_preserves_structured_downloads(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, include: bool, mode: str) -> None:
    from agent.component import message as module

    item = _download(include)
    message = object.__new__(Message)
    message._param = MessageParam()
    message._param.stream = mode != "plain"
    selected = "{% if 1 %}{DocGenerator:one@download}{% endif %}" if mode == "jinja" else "{DocGenerator:one@download}"
    message._param.content = ["UNSELECTED {DocGenerator:other@download}", selected]
    reads: list[str] = []

    def variable(name: str) -> Any:
        reads.append(name)
        assert "other" not in name
        return json.dumps(item) if name == "DocGenerator:one@download" else None

    message._canvas = SimpleNamespace(get_variable_value=variable, get_component_name=lambda name: "Report")
    monkeypatch.setattr(module.random, "choice", lambda choices: choices[1])
    monkeypatch.setattr(message, "check_if_canceled", lambda context: False)
    monkeypatch.setattr(message, "_convert_content", lambda content: None)
    monkeypatch.setattr(message, "_save_to_memory", AsyncMock() if mode == "stream" else lambda content: None)
    with caplog.at_level(logging.DEBUG):
        message._invoke()
        content = message.output("content")
        if mode == "stream":
            assert isinstance(content, partial)
            content = "".join([part async for part in content()])
    expected = {k: v for k, v in item.items() if k != "include_download_info_in_content"}
    assert message.output("downloads") == [expected]
    assert json.loads(content) == expected if include else content == ""
    assert item["base64"] not in caplog.text and item["filename"] not in caplog.text
    assert "DocGenerator:one@download" in reads


def test_missing_flag_remains_attachment_only() -> None:
    item = _download(False)
    del item["include_download_info_in_content"]
    message = object.__new__(Message)
    downloads: list[dict[str, Any]] = []
    assert message._stringify_message_value(item, downloads=downloads) == ""
    assert downloads == [item]
    assert DocGeneratorParam().include_download_info_in_content is False


def test_xlsx_conversion_does_not_log_download_payload(caplog: pytest.LogCaptureFixture) -> None:
    message = object.__new__(Message)
    message._param = MessageParam()
    message._param.output_format = "xlsx"
    with caplog.at_level(logging.INFO):
        message._convert_content(json.dumps(_download(True)))
    assert "AP/+" not in caplog.text and "document-1" not in caplog.text
