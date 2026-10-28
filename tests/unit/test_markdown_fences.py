"""Fenced examples survive the production Markdown table/extractor pipeline."""

import pytest

from core.app.naive import Markdown
from deepdoc.parser.markdown_parser import MarkdownElementExtractor, RAGFlowMarkdownParser


@pytest.mark.parametrize("marker", ["`", "~"])
@pytest.mark.parametrize("include_meta", [False, True])
def test_newline_delimiter_preserves_fence_and_line_metadata(marker: str, include_meta: bool) -> None:
    fence = f"{marker * 3}python\nprint('a')\nprint('b')\n{marker * 4}"
    sections = MarkdownElementExtractor(f"Before\n{fence}\nAfter").extract_elements(delimiter="`\n`", include_meta=include_meta)
    if include_meta:
        assert sections == [
            {"content": "Before", "start_line": 0, "end_line": 0},
            {"content": fence, "start_line": 1, "end_line": 4},
            {"content": "After", "start_line": 5, "end_line": 5},
        ]
    else:
        assert sections == ["Before", fence, "After"]


@pytest.mark.parametrize("marker", ["`", "~"])
@pytest.mark.parametrize("delimiter", [None, "`\n`"])
def test_shorter_or_other_fence_does_not_close_outer(marker: str, delimiter: str | None) -> None:
    other = "~" if marker == "`" else "`"
    fence = f"{marker * 4}markdown\n{marker * 3}\n{other * 4}\n{marker * 4}not-a-close\nbody\n{marker * 5}"
    sections = MarkdownElementExtractor(f"{fence}\nAfter").extract_elements(delimiter=delimiter, include_meta=True)
    assert [section["content"] for section in sections] == [fence, "After"]
    assert sections[0]["start_line"] == 0
    assert sections[0]["end_line"] == 5
    if delimiter is None:
        assert sections[0]["type"] == "code_block"


def test_unclosed_fence_preserves_everything_to_eof() -> None:
    fence = "   ~~~text\ninside\n| A | B |\n| --- | --- |\n| 1 | 2 |"
    remainder, tables = RAGFlowMarkdownParser().extract_tables_and_remainder(fence)
    assert remainder == fence
    assert tables == []
    assert MarkdownElementExtractor(fence).extract_elements("`\n`") == [fence.strip()]


@pytest.mark.parametrize("separate_tables", [False, True])
@pytest.mark.parametrize("marker", ["`", "~"])
@pytest.mark.parametrize(
    "example",
    [
        "| Name | Value |\n| --- | --- |\n| example | 1 |",
        "Name | Value\n--- | ---\nexample | 1",
        '<table border="1"><tr class="row"><td>A</td></tr></table>',
        "<table>\n<tr><td>A</td></tr>\n</table>",
    ],
)
def test_production_markdown_keeps_fenced_examples_and_real_tables(marker: str, example: str, separate_tables: bool) -> None:
    fence = f"{marker * 3}markdown\n{example}\n{marker * 4}"
    real_table = "| Real | Value |\n| --- | --- |\n| outside | 9 |"
    source = f"Before\n{fence}\n\n{real_table}\n\nAfter"

    remainder, raw_tables = RAGFlowMarkdownParser().extract_tables_and_remainder(source + "\n", separate_tables=separate_tables)
    assert fence in remainder
    assert len(raw_tables) == 1
    assert "outside" in raw_tables[0]

    sections, tables = Markdown()("guide.md", binary=source.encode(), separate_tables=separate_tables, delimiter="`\n`")
    contents = [section[0] for section in sections]
    assert fence in contents
    assert len(tables) == 1
    assert "outside" in tables[0][0][1]
    assert any("outside" in content for content in contents) is not separate_tables


@pytest.mark.parametrize("delimiter", [None, "`\n`"])
def test_real_table_after_longer_close_is_extracted(delimiter: str | None) -> None:
    fence = "~~~text\nexample\n~~~~"
    source = fence + '\n\n<table border="1"><tr><td>outside</td></tr></table>\n'
    sections, tables = Markdown()("guide.md", binary=source.encode(), delimiter=delimiter)
    assert [section[0] for section in sections] == [fence]
    assert len(tables) == 1
    assert 'border="1"' not in tables[0][0][1]


def test_delimiter_spanning_fence_start_cannot_remove_opener() -> None:
    source = "Before\n~~~python\nbody\n~~~\nAfter"
    sections = MarkdownElementExtractor(source).extract_elements(delimiter="`\n~~~` `\n`")
    assert any("~~~python\nbody\n~~~" in section for section in sections)


def test_crlf_fence_retains_offsets_and_outside_table() -> None:
    fence = '```html\r\n<table border="1"><tr><td>example</td></tr></table>\r\n````'
    source = f"Before\r\n{fence}\r\n\r\n| Real | Value |\r\n| --- | --- |\r\n| outside | 9 |\r\n"
    sections, tables = Markdown()("guide.md", binary=source.encode(), delimiter="`\n`")
    assert fence in [section[0] for section in sections]
    assert len(tables) == 1
    assert "outside" in tables[0][0][1]
    elements = MarkdownElementExtractor(source).extract_elements("`\n`", include_meta=True)
    assert elements[1] == {"content": fence, "start_line": 1, "end_line": 3}
