"""PDF page-range regression tests using small, generated documents."""

from io import BytesIO
from typing import Any

import pdfplumber
from reportlab.pdfgen import canvas

from common.constants import MAXIMUM_PAGE_NUMBER, MAXIMUM_TASK_PAGE_NUMBER
from deepdoc.parser.pdf_parser import PlainParser, RAGFlowPdfParser, VisionParser


def _make_pdf(page_count: int) -> bytes:
    output = BytesIO()
    writer = canvas.Canvas(output, pagesize=(72, 72))
    for page_number in range(1, page_count + 1):
        writer.drawString(4, 30, f"PAGE{page_number}")
        writer.showPage()
    writer.save()
    return output.getvalue()


def _render_pages(blob: bytes, *, from_page: int = 0, to_page: int = MAXIMUM_PAGE_NUMBER) -> RAGFlowPdfParser:
    parser = RAGFlowPdfParser.__new__(RAGFlowPdfParser)
    parser.parallel_limiter = None

    def capture_ocr(*_args: Any) -> None:
        # Keep real PDF rendering and text extraction; skip model inference.
        parser.boxes.append([])

    parser._RAGFlowPdfParser__ocr = capture_ocr
    parser.__images__(blob, zoomin=0.25, page_from=from_page, page_to=to_page)
    return parser


def test_large_pdf_reaches_page_302() -> None:
    blob = _make_pdf(302)

    parser = _render_pages(blob)
    assert len(parser.page_images) == len(parser.page_chars) == 302
    assert "PAGE302" in "".join(char["text"] for char in parser.page_chars[-1])

    sections, _ = PlainParser()(blob)
    assert any("PAGE302" in text for text, _ in sections)
    assert MAXIMUM_TASK_PAGE_NUMBER > MAXIMUM_PAGE_NUMBER


def test_explicit_pdf_range_and_short_pdf() -> None:
    long_pdf = _make_pdf(302)
    parser = _render_pages(long_pdf, from_page=300, to_page=302)
    assert len(parser.page_images) == 2
    assert "PAGE301" in "".join(char["text"] for char in parser.page_chars[0])
    assert "PAGE302" in "".join(char["text"] for char in parser.page_chars[1])

    sections, _ = PlainParser()(long_pdf, from_page=300, to_page=302)
    assert any("PAGE301" in text for text, _ in sections)
    assert any("PAGE302" in text for text, _ in sections)
    assert not any("PAGE300" in text for text, _ in sections)

    short_pdf = _make_pdf(2)
    assert len(_render_pages(short_pdf).page_images) == 2


def test_character_fallback_uses_actual_page_count(monkeypatch: Any) -> None:
    def failed_dedupe(_page: Any) -> None:
        raise RuntimeError("synthetic extraction failure")

    monkeypatch.setattr(pdfplumber.page.Page, "dedupe_chars", failed_dedupe)
    parser = _render_pages(_make_pdf(2))
    assert len(parser.page_images) == len(parser.page_chars) == 2
    assert parser.page_chars == [[], []]


def test_vision_parser_preserves_selected_page_numbers(monkeypatch: Any) -> None:
    from core.app import picture

    monkeypatch.setattr(picture, "vision_llm_chunk", lambda **_kwargs: "vision result")
    parser = VisionParser.__new__(VisionParser)
    parser.vision_model = object()
    sections, _ = parser(_make_pdf(302), from_page=300, to_page=302, zoomin=0.25)

    assert len(sections) == 2
    assert sections[0][1].startswith("@@301\t")
    assert sections[1][1].startswith("@@302\t")
