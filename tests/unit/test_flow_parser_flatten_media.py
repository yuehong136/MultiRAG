from types import SimpleNamespace
from typing import Any

import pytest

from core.flow.parser.parser import Parser, ParserParam


def _make_parser(file_type: str) -> Parser:
    param = ParserParam()
    param.setups[file_type]["output_format"] = "json"
    param.setups[file_type]["flatten_media_to_text"] = True
    parser = Parser.__new__(Parser)
    parser._param = param
    parser._canvas = SimpleNamespace(_tenant_id="tenant-1")
    parser.callback = lambda *args, **kwargs: None
    parser._id = "parser-1"
    return parser


def _capture_media_types(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import core.flow.parser.parser as parser_module

    captured: list[str] = []

    def capture(sections: list[dict[str, Any]], *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        captured.extend(section["doc_type_kwd"] for section in sections if section.get("doc_type_kwd") in {"image", "table"})
        return sections

    monkeypatch.setattr(parser_module, "enhance_media_sections_with_vision", capture)
    return captured


def test_parser_defaults_keep_media_classification_enabled() -> None:
    setups = ParserParam().setups

    assert setups["pdf"]["flatten_media_to_text"] is False
    assert setups["spreadsheet"]["flatten_media_to_text"] is False
    assert setups["docx"]["flatten_media_to_text"] is False
    assert setups["markdown"]["flatten_media_to_text"] is False


def test_pdf_flatten_media_to_text_skips_vision_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.flow.parser.parser as parser_module

    class FakePdfParser:
        outlines: list[Any] = []

        def parse_into_bboxes(self, _blob: bytes, callback: Any) -> list[dict[str, Any]]:
            return [
                {"text": "table", "layout_type": "table"},
                {"text": "figure", "layout_type": "figure", "image": object()},
            ]

    monkeypatch.setattr(parser_module, "RAGFlowPdfParser", FakePdfParser)
    captured = _capture_media_types(monkeypatch)
    parser = _make_parser("pdf")

    parser._pdf("sample.pdf", b"pdf")

    assert captured == []
    assert [item["doc_type_kwd"] for item in parser.output("json")] == ["text", "text"]


def test_spreadsheet_flatten_media_to_text_marks_tables_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.flow.parser.parser as parser_module

    class FakeSpreadsheetParser:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def check_installation(self) -> bool:
            return True

        def parse_pdf(self, **_kwargs: Any) -> tuple[list[tuple[str, str]], list[str]]:
            return [("cell", "")], ["<table><tr><td>value</td></tr></table>"]

    monkeypatch.setattr(parser_module, "TCADPParser", FakeSpreadsheetParser)
    parser = _make_parser("spreadsheet")
    parser._param.setups["spreadsheet"]["parse_method"] = "tcadp parser"

    parser._spreadsheet("sample.xlsx", b"spreadsheet")

    assert [item["doc_type_kwd"] for item in parser.output("json")] == ["text", "text"]


def test_docx_flatten_media_to_text_skips_vision_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.flow.parser.parser as parser_module

    class FakeDocxParser:
        def __call__(self, _name: str, binary: bytes) -> list[tuple[str, object, str]]:
            return [("figure", object(), "<table><tr><td>value</td></tr></table>")]

    monkeypatch.setattr(parser_module, "Docx", FakeDocxParser)
    monkeypatch.setattr(parser_module, "extract_word_outlines", lambda *_args, **_kwargs: [])
    captured = _capture_media_types(monkeypatch)
    parser = _make_parser("docx")

    parser._docx("sample.docx", b"docx")

    assert captured == []
    assert [item["doc_type_kwd"] for item in parser.output("json")] == ["text", "text"]


def test_markdown_flatten_media_to_text_marks_tables_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_media_types(monkeypatch)
    parser = _make_parser("markdown")
    markdown = b"Intro.\n\n| Name | Value |\n| --- | --- |\n| A | 1 |\n"

    parser._markdown("sample.md", markdown)

    assert captured == []
    assert {item["doc_type_kwd"] for item in parser.output("json")} == {"text"}
