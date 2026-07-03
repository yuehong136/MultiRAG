from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from core.flow.parser.parser import Parser, ParserParam


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
