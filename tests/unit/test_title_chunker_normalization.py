"""Format normalization through the real title chunker dispatch and strategies."""

from collections.abc import Iterator
from typing import Any

import pytest
from PIL import Image

from agent.canvas import Graph
from core.flow.chunker.title_chunker import TitleChunker, TitleChunkerParam
from core.flow.parser.pdf_chunk_metadata import PDF_POSITIONS_KEY


@pytest.fixture(params=["hierarchy", "group"])
def chunker(request: pytest.FixtureRequest) -> Iterator[TitleChunker]:
    graph = Graph('{"components": {}}', tenant_id="line-e-unit")
    graph._doc_id = None
    param = TitleChunkerParam()
    param.method = request.param
    param.hierarchy = 1
    param.levels = [[r"^\d+ "]]
    try:
        yield TitleChunker(graph, "title-normalization", param)
    finally:
        graph._thread_pool.shutdown(wait=True)


@pytest.mark.parametrize(
    ("output_format", "payload", "expected"),
    [
        ("text", "  Alpha  \n \t \n\tBeta\t\n\n", "Alpha\nBeta\n"),
        ("markdown", "    code()  \n \t \n\t- nested item  \n", "    code()  \n\t- nested item  \n"),
        ("html", "  <pre>\n\t  value  \n \t \n  </pre>  \n", "  <pre>\n\t  value  \n  </pre>  \n"),
    ],
)
async def test_text_formats_clean_blanks_and_preserve_formatting(chunker: TitleChunker, output_format: str, payload: str, expected: str) -> None:
    result = await chunker.invoke(name="source.txt", output_format=output_format, **{output_format: payload})

    assert "_ERROR" not in result
    assert result["output_format"] == "chunks"
    assert result["chunks"] == [{"text": expected}]


@pytest.mark.parametrize("output_format", ["text", "markdown", "html"])
@pytest.mark.parametrize("payload", [None, "", "  \t\n\u3000\n"])
async def test_blank_text_inputs_do_not_emit_chunks(chunker: TitleChunker, output_format: str, payload: str | None) -> None:
    result = await chunker.invoke(name="empty.txt", output_format=output_format, **{output_format: payload})

    assert "_ERROR" not in result
    assert result["chunks"] == []


@pytest.mark.parametrize("output_format", ["json", "chunks"])
@pytest.mark.parametrize("payload", [None, []])
async def test_empty_structured_inputs_do_not_fall_into_text_branch(chunker: TitleChunker, output_format: str, payload: list[dict[str, Any]] | None) -> None:
    result = await chunker.invoke(name="empty.pdf", output_format=output_format, **{output_format: payload})

    assert "_ERROR" not in result
    assert result["chunks"] == []


@pytest.mark.parametrize("output_format", ["json", "chunks"])
async def test_structured_outputs_keep_text_images_and_pdf_positions(chunker: TitleChunker, output_format: str) -> None:
    with Image.new("RGB", (2, 3), "red") as image:
        items = [
            {"text": "1 Intro @@1\t10\t20\t30\t40##", "doc_type_kwd": "text", PDF_POSITIONS_KEY: [[1, 10, 20, 30, 40]]},
            {"text": None, "doc_type_kwd": "image", "img_id": "tenant/image-1", "image": image, PDF_POSITIONS_KEY: [[2, 5, 15, 25, 35]]},
            {"text": "2 Next", "doc_type_kwd": "text", PDF_POSITIONS_KEY: [[3, 12, 22, 32, 42]]},
        ]
        result = await chunker.invoke(name="source.pdf", output_format=output_format, **{output_format: items})

        assert "_ERROR" not in result
        chunks = result["chunks"]
        assert len(chunks) == 3
        assert [item["doc_type_kwd"] for item in chunks] == ["text", "image", "text"]
        assert chunks[0]["text"] == "1 Intro \n"
        assert chunks[0]["position_int"] == [(1, 10, 20, 30, 40)]
        assert chunks[1]["text"] == ""
        assert chunks[1]["img_id"] == "tenant/image-1"
        assert chunks[1]["image"].size == image.size
        assert chunks[1]["image"].tobytes() == image.tobytes()
        assert chunks[1]["position_int"] == [(2, 5, 15, 25, 35)]
        assert chunks[2]["text"] == "2 Next\n"
        assert chunks[2]["position_int"] == [(3, 12, 22, 32, 42)]
        assert [chunk["page_num_int"] for chunk in chunks] == [[1], [2], [3]]
        assert all(PDF_POSITIONS_KEY not in chunk for chunk in chunks)
        assert items[1]["text"] is None  # normalization must not mutate upstream records
        chunks[1]["image"].close()


@pytest.mark.parametrize("output_format", ["json", "chunks"])
async def test_structured_root_heading_is_preserved(chunker: TitleChunker, output_format: str) -> None:
    chunker._param.root_chunk_as_heading = True
    root = "Document root " + "context " * 64
    body = "content " * 64
    items = [{"text": text, "doc_type_kwd": "text"} for text in [root, "1 First", body, "2 Second", body]]

    result = await chunker.invoke(name="source.pdf", output_format=output_format, **{output_format: items})

    assert "_ERROR" not in result
    chunks = result["chunks"]
    assert len(chunks) == 2
    assert chunks[0]["text"] == f"{root}\n\n1 First\n{body}\n"
    assert chunks[1]["text"] == f"{root}\n\n2 Second\n{body}\n"
