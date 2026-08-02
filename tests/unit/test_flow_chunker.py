import asyncio
from types import SimpleNamespace

import pytest

from core.flow.chunker.title_chunker import TitleChunker, TitleChunkerParam
from core.flow.chunker.token_chunker import TokenChunker, TokenChunkerParam
from core.flow.parser.pdf_chunk_metadata import PDF_POSITIONS_KEY


def _make_process(component_cls, param):
    process = component_cls.__new__(component_cls)
    process._param = param
    process._canvas = SimpleNamespace(_doc_id=None, _tenant_id="tenant-1")
    process.callback = lambda *args, **kwargs: None
    process._id = "component-1"
    return process


def _outputs(process):
    return {key: value["value"] for key, value in process._param.outputs.items()}


def test_token_chunker_splits_text_with_delimiters_and_children():
    param = TokenChunkerParam()
    param.delimiters = ["`##`"]
    param.children_delimiters = ["@@"]
    param.check()
    chunker = _make_process(TokenChunker, param)

    asyncio.run(
        chunker._invoke(
            name="sample.txt",
            output_format="text",
            text="Intro##Body@@Child",
        )
    )

    chunks = _outputs(chunker)["chunks"]
    assert _outputs(chunker)["output_format"] == "chunks"
    assert chunks == [
        {"text": "Intro##", "mom": "Intro##"},
        {"text": "Body@@", "mom": "Body@@Child"},
        {"text": "Child", "mom": "Body@@Child"},
    ]


def test_token_chunker_one_mode_keeps_text_as_single_chunk():
    param = TokenChunkerParam()
    param.delimiter_mode = "one"
    param.check()
    chunker = _make_process(TokenChunker, param)

    asyncio.run(
        chunker._invoke(
            name="sample.txt",
            output_format="text",
            text="Intro##Body@@Child",
        )
    )

    assert _outputs(chunker)["chunks"] == [{"text": "Intro##Body@@Child"}]


def test_token_chunker_one_mode_merges_json_sections_into_single_chunk():
    param = TokenChunkerParam()
    param.delimiter_mode = "one"
    param.check()
    chunker = _make_process(TokenChunker, param)

    asyncio.run(
        chunker._invoke(
            name="sample.pdf",
            output_format="json",
            json=[
                {"text": "First section.", "doc_type_kwd": "text"},
                {"doc_type_kwd": "text", "content_with_weight": "Legacy section."},
                {"text": "   ", "doc_type_kwd": "text"},
                {"text": "Last section.", "doc_type_kwd": "table"},
            ],
        )
    )

    assert _outputs(chunker)["chunks"] == [{"text": "First section.\nLegacy section.\nLast section."}]


def test_token_chunker_param_rejects_unknown_delimiter_mode():
    param = TokenChunkerParam()
    param.delimiter_mode = "sentence"

    with pytest.raises(ValueError, match=r"Delimiter mode abnormal\."):
        param.check()


def test_token_chunker_preserves_media_types_context_and_pdf_fields():
    param = TokenChunkerParam()
    param.delimiters = []
    param.children_delimiters = []
    param.chunk_token_size = 1024
    param.table_context_size = 16
    param.image_context_size = 16
    param.check()
    chunker = _make_process(TokenChunker, param)

    asyncio.run(
        chunker._invoke(
            name="sample.pdf",
            output_format="json",
            json=[
                {
                    "text": "Before table.",
                    "doc_type_kwd": "text",
                    PDF_POSITIONS_KEY: [[0, 10, 20, 30, 40]],
                },
                {"text": "Table body", "doc_type_kwd": "table"},
                {"text": "Image caption", "doc_type_kwd": "image", "img_id": "img-1"},
                {"text": "After image.", "doc_type_kwd": "text"},
            ],
        )
    )

    chunks = _outputs(chunker)["chunks"]

    assert chunks[0]["doc_type_kwd"] == "text"
    assert chunks[0]["position_int"] == [(1, 10, 20, 30, 40)]
    assert chunks[0]["page_num_int"] == [1]
    assert PDF_POSITIONS_KEY not in chunks[0]
    assert chunks[1]["doc_type_kwd"] == "table"
    assert "Before table." in chunks[1]["text"]
    assert chunks[2]["doc_type_kwd"] == "image"
    assert chunks[2]["img_id"] == "img-1"
    assert "After image." in chunks[2]["text"]


def test_title_chunker_defaults_to_hierarchy_with_frequency_fallback():
    param = TitleChunkerParam()
    param.hierarchy = 1
    param.levels = [[r"^\d+ "]]
    chunker = _make_process(TitleChunker, param)

    asyncio.run(
        chunker._invoke(
            name="sample.md",
            output_format="markdown",
            markdown="1 Intro\nBody\n2 Next\nMore",
        )
    )

    chunks = _outputs(chunker)["chunks"]
    assert param.method == "hierarchy"
    assert _outputs(chunker)["output_format"] == "chunks"
    assert chunks == [{"text": "1 Intro\nBody\n"}, {"text": "2 Next\nMore\n"}]


def test_title_chunker_group_uses_outlines_when_available():
    param = TitleChunkerParam()
    param.method = "group"
    param.hierarchy = 1
    param.levels = [[r"^\d+ "]]
    chunker = _make_process(TitleChunker, param)
    long_body = " ".join(f"word{i}" for i in range(80))

    asyncio.run(
        chunker._invoke(
            name="sample.pdf",
            file={"outlines": [("Intro", 0, 1), ("Next", 0, 2)]},
            output_format="json",
            json=[
                {"text": "Intro", "doc_type_kwd": "text"},
                {"text": long_body, "doc_type_kwd": "text"},
                {"text": "Next", "doc_type_kwd": "text"},
                {"text": long_body, "doc_type_kwd": "text"},
            ],
        )
    )

    chunks = _outputs(chunker)["chunks"]
    assert len(chunks) == 2
    assert chunks[0]["text"].startswith("Intro\n")
    assert chunks[1]["text"].startswith("Next\n")


def test_title_chunker_can_promote_root_chunk_to_heading():
    param = TitleChunkerParam()
    param.hierarchy = 1
    param.levels = [[r"^\d+ "]]
    param.root_chunk_as_heading = True
    chunker = _make_process(TitleChunker, param)

    asyncio.run(
        chunker._invoke(
            name="sample.pdf",
            output_format="json",
            json=[
                {"text": "Document root", "doc_type_kwd": "text"},
                {"text": "1 First", "doc_type_kwd": "text"},
                {"text": "First body", "doc_type_kwd": "text"},
                {"text": "2 Second", "doc_type_kwd": "text"},
                {"text": "Second body", "doc_type_kwd": "text"},
            ],
        )
    )

    chunks = _outputs(chunker)["chunks"]
    assert len(chunks) == 2
    assert chunks[0]["text"] == "Document root\n\n1 First\nFirst body\n"
    assert chunks[1]["text"] == "Document root\n\n2 Second\nSecond body\n"
