import asyncio
from copy import deepcopy
from io import BytesIO
from typing import Any

import pytest
from PIL import Image

from core.flow.utils import FlowParser, extract_metadata, parse_file
from core.flow.utils.parser_utils import clone_parser_setups


class _FakeOCR:
    def __call__(self, _image_array):
        return [([0, 0, 1, 1], ("detected text", 0.99))]


class _FakeLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> str:
        self.calls.append({"system": system, "history": deepcopy(history), "gen_conf": deepcopy(gen_conf), "kwargs": deepcopy(kwargs)})
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


def test_parser_utils_routes_txt_to_text_code_runtime_parser():
    result = asyncio.run(
        parse_file(
            "notes.txt",
            b"line one\nline two\n",
            tenant_id="tenant-1",
        )
    )

    assert result["output_format"] == "json"
    assert result["json"]
    assert all(item["doc_type_kwd"] == "text" for item in result["json"])
    assert "line one" in result["json"][0]["text"]


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
            "word": {"output_format": "markdown"},
            "audio": {"llm_id": "audio-model"},
            "video": {"llm_id": "video-model", "prompt": "describe"},
        }
    )

    assert setups["image"]["parse_method"] == "ocr"
    assert setups["html"]["remove_toc"] == "true"
    assert setups["text&code"]["output_format"] == "json"
    assert setups["doc"]["output_format"] == "markdown"
    assert setups["docx"]["output_format"] == "markdown"
    assert setups["audio"]["vlm"] == {"llm_id": "audio-model"}
    assert setups["video"]["vlm"] == {"llm_id": "video-model"}
    assert setups["video"]["prompt"] == "describe"
    assert "txt" in setups["text&code"]["suffix"]
    assert "txt" not in setups["markdown"]["suffix"]


def test_parser_utils_preserves_canonical_media_vlm_configs() -> None:
    setups = clone_parser_setups(
        parser_config={
            "audio": {"vlm": {"llm_id": "audio-model"}},
            "video": {"vlm": {"llm_id": "video-model"}, "prompt": "describe"},
        }
    )

    assert setups["audio"]["vlm"] == {"llm_id": "audio-model"}
    assert setups["video"]["vlm"] == {"llm_id": "video-model"}
    assert setups["video"]["prompt"] == "describe"


async def test_extractor_utils_reuses_runtime_extractor_async_flow() -> None:
    model = _FakeLLM()
    chunks = [{"content_with_weight": "chunk body", "source": "keep"}, {"text": "second body", "nested": {"zero": 0}}]
    original = deepcopy(chunks)
    result = await extract_metadata(
        chunks,
        field_name="topic",
        prompt="classify",
        llm_model=model,
        temperature=0.2,
    )

    assert result == [
        {
            "content_with_weight": "chunk body",
            "source": "keep",
            "text": "chunk body",
            "topic": "classify:chunk body:0.2",
        },
        {"text": "second body", "nested": {"zero": 0}, "topic": "classify:second body:0.2"},
    ]
    assert chunks == original
    assert [call["history"][-1]["content"] for call in model.calls] == ["chunk body", "second body"]
    assert all(call["kwargs"] == {} for call in model.calls)


async def test_extractor_utils_empty_chunks_skip_generation() -> None:
    model = _FakeLLM()
    result = await extract_metadata([], field_name="topic", prompt="classify", llm_model=model)

    assert result == []
    assert model.calls == []


async def test_extractor_utils_propagates_generation_failure_without_mutating_input() -> None:
    class FailingLLM(_FakeLLM):
        async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> str:
            answer = await super().async_chat(system, history, gen_conf, **kwargs)
            if len(self.calls) == 2:
                raise RuntimeError("fixture generation failed")
            return answer

    model = FailingLLM()
    chunks = [{"content_with_weight": "first"}, {"text": "second", "nested": {"zero": 0}}]
    original = deepcopy(chunks)
    with pytest.raises(RuntimeError, match="fixture generation failed"):
        await extract_metadata(chunks, field_name="topic", prompt="classify", llm_model=model)
    assert len(model.calls) == 2
    assert chunks == original
