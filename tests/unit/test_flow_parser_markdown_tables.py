from types import SimpleNamespace

from core.flow.parser.parser import Parser, ParserParam

_MD = "Intro paragraph.\n\n| Name | Value |\n| --- | --- |\n| A | 1 |\n\nOutro paragraph.\n"


def _make_markdown_parser(output_format, monkeypatch):
    import core.flow.parser.parser as parser_module

    monkeypatch.setattr(parser_module, "enhance_media_sections_with_vision", lambda *args, **kwargs: None)

    param = ParserParam()
    param.setups["markdown"]["output_format"] = output_format
    parser = Parser.__new__(Parser)
    parser._param = param
    parser._canvas = SimpleNamespace(_tenant_id="tenant-1")
    parser.callback = lambda *args, **kwargs: None
    parser._id = "parser-1"
    return parser


def test_markdown_json_output_emits_table_chunk_once(monkeypatch):
    parser = _make_markdown_parser("json", monkeypatch)

    parser._markdown("sample.md", _MD.encode("utf-8"))

    chunks = parser.output("json")
    table_chunks = [chunk for chunk in chunks if chunk["doc_type_kwd"] == "table"]
    text_chunks = [chunk for chunk in chunks if chunk["doc_type_kwd"] == "text"]

    assert len(table_chunks) == 1
    assert "<table>" in table_chunks[0]["text"]
    assert any("Intro paragraph." in chunk["text"] for chunk in text_chunks)
    assert all("| Name | Value |" not in chunk["text"] for chunk in text_chunks)


def test_markdown_text_output_appends_table_html(monkeypatch):
    parser = _make_markdown_parser("text", monkeypatch)

    parser._markdown("sample.md", _MD.encode("utf-8"))

    text = parser.output("text")
    assert "Intro paragraph." in text
    assert "Outro paragraph." in text
    assert text.count("<table>") == 1
    assert "| Name | Value |" not in text
