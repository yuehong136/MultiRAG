import sys
from io import BytesIO
from types import ModuleType, SimpleNamespace

import pytest
from PIL import Image

from core.flow.parser.parser import Parser, ParserParam
from core.flow.utils.parser_utils import parse_file
from core.flow.utils.token_chunker_utils import split_chunks


class _FakeOCR:
    def __call__(self, _image_array):
        return [([0, 0, 1, 1], ("detected text", 0.99))]


def _make_png_bytes():
    buf = BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_parser_image_outputs_json_image_chunk(monkeypatch):
    import deepdoc.vision

    monkeypatch.setattr(deepdoc.vision, "OCR", _FakeOCR)

    param = ParserParam()
    parser = Parser.__new__(Parser)
    parser._param = param
    parser._canvas = SimpleNamespace(get_tenant_id=lambda: "tenant-1")
    parser.callback = lambda *args, **kwargs: None
    parser._id = "parser-1"

    parser._image("sample.png", _make_png_bytes())

    assert parser.output("output_format") == "json"
    assert parser.output("json") == [
        {
            "text": "detected text",
            "image": parser.output("json")[0]["image"],
            "doc_type_kwd": "image",
        }
    ]


def test_parser_doc_parses_with_tika(monkeypatch):
    tika_module = ModuleType("tika")
    tika_parser = SimpleNamespace(from_buffer=lambda _buffer: {"content": "Title\n\nBody"})
    tika_module.parser = tika_parser
    monkeypatch.setitem(sys.modules, "tika", tika_module)

    param = ParserParam()
    param.setups["doc"]["output_format"] = "markdown"
    param.setups["doc"]["remove_toc"] = True
    parser = Parser.__new__(Parser)
    parser._param = param
    parser._canvas = SimpleNamespace(get_tenant_id=lambda: "tenant-1")
    parser.callback = lambda *args, **kwargs: None
    parser._id = "parser-1"

    parser._doc("legacy.doc", b"fake-doc")

    assert parser.output("file")["outlines"] == []
    assert parser.output("markdown") == "Title\n\nBody"


@pytest.mark.asyncio
async def test_parse_file_routes_doc_to_doc_parser(monkeypatch):
    tika_module = ModuleType("tika")
    tika_parser = SimpleNamespace(from_buffer=lambda _buffer: {"content": "Legacy\nDoc"})
    tika_module.parser = tika_parser
    monkeypatch.setitem(sys.modules, "tika", tika_module)

    parsed = await parse_file(
        "legacy.doc",
        b"fake-doc",
        tenant_id="tenant-1",
        word_config={"output_format": "markdown", "remove_toc": True},
    )

    assert parsed["file"]["outlines"] == []
    assert parsed["markdown"] == "Legacy\n\nDoc"


@pytest.mark.parametrize("output_format", ["text", "markdown", "json"])
async def test_image_non_json_configuration_reaches_real_chunk_consumer(monkeypatch: pytest.MonkeyPatch, output_format: str) -> None:
    import deepdoc.vision

    monkeypatch.setattr(deepdoc.vision, "OCR", _FakeOCR)
    parsed = await parse_file("sample.png", _make_png_bytes(), "tenant-1", image_config={"output_format": output_format, "parse_method": "ocr"})
    try:
        assert parsed["output_format"] == "json"
        chunks = await split_chunks(parsed)
        assert len(chunks) == 1 and chunks[0]["text"] == "detected text"
        assert chunks[0]["doc_type_kwd"] == "image" and isinstance(chunks[0]["image"], Image.Image)
    finally:
        parsed["json"][0]["image"].close()


@pytest.mark.parametrize("prompt", [None, "describe details"])
async def test_image_vlm_keeps_usable_image_and_closes_binary_buffers(monkeypatch: pytest.MonkeyPatch, prompt: str | None) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager
    from typing import Any

    from core.flow.parser import parser as parser_module

    binary = _make_png_bytes()
    streams: list[BytesIO] = []
    stream_type = BytesIO

    class TrackedBuffer(stream_type):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            streams.append(self)

    @contextmanager
    def db() -> Iterator[None]:
        yield None

    class CV:
        def describe(self, content: bytes) -> str:
            assert content.startswith(b"\xff\xd8")
            return "vision description"

        def describe_with_prompt(self, content: bytes, supplied_prompt: str) -> str:
            assert supplied_prompt == prompt
            return self.describe(content)

    monkeypatch.setattr(parser_module.io, "BytesIO", TrackedBuffer)
    monkeypatch.setattr(parser_module, "db_connection", db)
    monkeypatch.setattr(parser_module, "get_model_config_by_type_and_name", lambda *args: {})
    monkeypatch.setattr(parser_module, "LLMBundle", lambda *args, **kwargs: CV())
    parsed = await parse_file("sample.png", binary, "tenant-1", image_config={"parse_method": "test-vlm", "output_format": "markdown", "system_prompt": prompt})
    try:
        assert parsed["output_format"] == "json"
        assert all(stream.closed for stream in streams)
        chunks = await split_chunks(parsed)
        assert chunks[0]["text"] == "vision description"
        assert chunks[0]["image"].getpixel((0, 0)) == (255, 255, 255)
    finally:
        parsed["json"][0]["image"].close()
