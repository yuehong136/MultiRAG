import asyncio
from io import BytesIO

from PIL import Image

from core.flow.utils import FlowParser, extract_metadata, parse_file
from core.flow.utils.parser_utils import clone_parser_setups


class _FakeOCR:
    def __call__(self, _image_array):
        return [([0, 0, 1, 1], ("detected text", 0.99))]


class _FakeLLM:
    async def async_chat(self, system, history, gen_conf, **_kwargs):
        return f"{system}:{history[-1]['content']}:{gen_conf.get('temperature')}"


def _make_png_bytes():
    buf = BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_parser_utils_routes_code_to_runtime_parser_json():
    result = asyncio.run(
        parse_file(
            "sample.py",
            b"print('hello')\n",
            tenant_id="tenant-1",
            code_config={"output_format": "json"},
        )
    )

    assert result["output_format"] == "json"
    assert result["json"] == [{"text": "print('hello')\n", "doc_type_kwd": "text"}]


def test_parser_utils_routes_html_to_runtime_parser_json():
    result = asyncio.run(
        parse_file(
            "sample.html",
            b"<html><body><h1>Title</h1><p>Body</p></body></html>",
            tenant_id="tenant-1",
            html_config={"output_format": "json"},
        )
    )

    assert result["output_format"] == "json"
    assert result["json"]
    assert all(item["doc_type_kwd"] == "text" for item in result["json"])


def test_parser_utils_image_uses_runtime_json_contract(monkeypatch):
    import deepdoc.vision

    monkeypatch.setattr(deepdoc.vision, "OCR", _FakeOCR)

    result = asyncio.run(
        FlowParser.parse_image(
            "sample.png",
            _make_png_bytes(),
            tenant_id="tenant-1",
            parse_method="deepdoc",
        )
    )

    assert result["output_format"] == "json"
    assert result["json"][0]["text"] == "detected text"
    assert result["json"][0]["doc_type_kwd"] == "image"
    assert isinstance(result["json"][0]["image"], Image.Image)


def test_parser_utils_normalizes_direct_configs():
    setups = clone_parser_setups(
        parser_config={
            "image": {"parse_method": "deepdoc"},
            "html": {"remove_toc": True},
            "code": {"output_format": "json"},
        }
    )

    assert setups["image"]["parse_method"] == "ocr"
    assert setups["html"]["remove_toc"] == "true"
    assert setups["code"]["output_format"] == "json"


def test_extractor_utils_reuses_runtime_extractor_async_flow():
    result = asyncio.run(
        extract_metadata(
            [{"content_with_weight": "chunk body", "source": "keep"}],
            field_name="topic",
            prompt="classify",
            llm_model=_FakeLLM(),
            temperature=0.2,
        )
    )

    assert result == [
        {
            "content_with_weight": "chunk body",
            "source": "keep",
            "text": "chunk body",
            "topic": "classify:chunk body:0.2",
        }
    ]


def test_extractor_utils_empty_chunks_skip_generation():
    result = asyncio.run(
        extract_metadata(
            [],
            field_name="topic",
            prompt="classify",
            llm_model=_FakeLLM(),
        )
    )

    assert result == []
