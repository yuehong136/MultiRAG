from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

import core.flow.parser.parser as parser_module
from core.flow.parser.parser import Parser, ParserParam


class _FakeBundle:
    def __init__(self, _db: Any, _tenant_id: str, _config: object) -> None:
        pass

    def transcription(self, _path: str) -> str:
        return "transcript"

    async def async_chat(self, **_kwargs: Any) -> str:
        return "video description"


def _make_parser(file_type: str, llm_id: str) -> Parser:
    param = ParserParam()
    param.setups[file_type]["vlm"] = {"llm_id": llm_id}
    parser = Parser.__new__(Parser)
    parser._param = param
    parser._canvas = SimpleNamespace(get_tenant_id=lambda: "tenant-1")
    parser.callback = lambda *_args, **_kwargs: None
    parser._id = "parser-1"
    return parser


def _patch_model_lookup(monkeypatch: pytest.MonkeyPatch, captured: list[tuple[str, str]]) -> None:
    @contextmanager
    def fake_db_connection() -> Iterator[object]:
        yield object()

    def fake_get_model_config(_db: Any, tenant_id: str, _model_type: str, llm_id: str) -> object:
        captured.append((tenant_id, llm_id))
        return object()

    monkeypatch.setattr(parser_module, "db_connection", fake_db_connection)
    monkeypatch.setattr(parser_module, "get_model_config_by_type_and_name", fake_get_model_config)
    monkeypatch.setattr(parser_module, "LLMBundle", _FakeBundle)


def test_audio_parser_resolves_nested_vlm_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str]] = []
    _patch_model_lookup(monkeypatch, captured)
    parser = _make_parser("audio", "audio-model")

    parser._audio("sample.wav", b"audio")

    assert captured == [("tenant-1", "audio-model")]
    assert parser.output("text") == "transcript"


def test_video_parser_resolves_nested_vlm_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str]] = []
    _patch_model_lookup(monkeypatch, captured)
    parser = _make_parser("video", "video-model")

    parser._video("sample.mp4", b"video")

    assert captured == [("tenant-1", "video-model")]
    assert parser.output("text") == "video description"
