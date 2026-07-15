import sys
from io import BytesIO
from types import ModuleType, SimpleNamespace

import pytest
from PIL import Image

from core.flow.parser.parser import Parser, ParserParam
from core.flow.utils.parser_utils import parse_file


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
