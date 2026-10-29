"""Custom delimiters preserve tables through the active Markdown parser chain."""

from contextlib import nullcontext
from typing import Any

import pytest
from PIL import Image

from core.app import naive
from deepdoc.parser.markdown_parser import MarkdownElementExtractor


@pytest.mark.parametrize(
    "table",
    [
        "| Name | Value |\n| --- | --- |\n| A | 1 |\n| B | 2 |",
        "Name | Value\n--- | ---\nA | 1\nB | 2",
        "| Name | Value |\n| :- | -: |\n| A | 1 |",
        "Name | Value\n- | -\nA | 1",
        "| Name | Value |\n| --- | --- |\n| | |\n| B | 2 |",
        "| | |\n| --- | --- |\n| A | 1 |",
        "Name | Value\n- | -",
        "<table>\n<tr><td>A</td></tr>\n<tr><td>B</td></tr>\n</table>",
        '<HTML lang="en"><BODY>\n<TABLE class="data">\n<TR><TD>A</TD></TR>\n</TABLE>\n</BODY></HTML>',
        "<body>\n<table>\n<tr><td>A</td></tr>\n</table>\n</body>",
    ],
)
@pytest.mark.parametrize("include_meta", [False, True])
def test_newline_delimiter_preserves_table_and_metadata(table: str, include_meta: bool) -> None:
    source = f"Before\n{table}\nAfter"
    sections = MarkdownElementExtractor(source).extract_elements("`\n`", include_meta=include_meta)
    if include_meta:
        end_line = table.count("\n") + 1
        assert sections == [
            {"content": "Before", "start_line": 0, "end_line": 0},
            {"content": table, "start_line": 1, "end_line": end_line},
            {"content": "After", "start_line": end_line + 1, "end_line": end_line + 1},
        ]
    else:
        assert sections == ["Before", table, "After"]


@pytest.mark.parametrize("delimiter", ["`|`", "`\n` `|`", "`\n|` `\n`"])
def test_pipe_or_spanning_delimiter_cannot_remove_table_structure(delimiter: str) -> None:
    table = "| Name | Value |\n| --- | --- |\n| A | 1 |"
    source = f"Before\n{table}\nAfter"
    sections = MarkdownElementExtractor(source).extract_elements(delimiter)
    assert any(table in section for section in sections)
    assert "Before" in sections[0]
    assert "After" in sections[-1]


def test_crlf_table_at_eof_retains_source_and_line_metadata() -> None:
    table = "Name | Value\r\n- | -\r\nA | 1"
    sections = MarkdownElementExtractor(f"Before\r\n{table}").extract_elements("`\n`", include_meta=True)
    assert sections == [{"content": "Before", "start_line": 0, "end_line": 0}, {"content": table, "start_line": 1, "end_line": 3}]


def test_non_table_pipe_text_still_obeys_delimiters() -> None:
    source = "Before\nA | B\nnot | separator\nAfter"
    assert MarkdownElementExtractor(source).extract_elements("`\n`") == source.splitlines()
    assert MarkdownElementExtractor("A|B").extract_elements("`|`") == ["A", "B"]


def test_html_matching_does_not_cross_fence_boundaries() -> None:
    fence = "```html\n<table>\n````"
    source = f"Before\n{fence}\nOutside one\nOutside two\n</table>\nAfter"
    assert MarkdownElementExtractor(source).extract_elements("`\n`") == ["Before", fence, "Outside one", "Outside two", "</table>", "After"]


@pytest.mark.parametrize("separate_tables", [False, True])
@pytest.mark.parametrize("real_table", ["| Real | Value |\n| --- | --- |\n| outside | 9 |", '<table border="1">\n<tr><td>outside</td></tr>\n</table>'])
def test_production_parser_keeps_fenced_examples_and_real_tables(real_table: str, separate_tables: bool) -> None:
    fence = '~~~~markdown\n| Fake | Table |\n| --- | --- |\n| code | only |\n<table border="1"><tr><td>example</td></tr></table>\n~~~~~'
    source = f"Before\n{fence}\n\n{real_table}\n\nAfter"
    sections, tables = naive.Markdown()("guide.md", binary=source.encode(), separate_tables=separate_tables, delimiter="`\n`")
    assert [section[0] for section in sections] == (["Before", fence, "After"] if separate_tables else ["Before", fence, real_table, "After"])
    assert len(tables) == 1
    assert "outside" in tables[0][0][1]
    assert "example" not in tables[0][0][1]


def test_table_images_remain_associated_with_their_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    images = {"table.png": Image.new("RGB", (2, 2), "red"), "after.png": Image.new("RGB", (2, 2), "blue")}

    def load_images(self: Any, urls: list[str], cache: Any = None) -> tuple[list[Image.Image], Any]:
        return [images[url] for url in urls], cache

    monkeypatch.setattr(naive.Markdown, "load_images_from_urls", load_images)
    table = "| Name | Image |\n| --- | --- |\n| A | ![table](table.png) |"
    source = f"Before\n{table}\nAfter ![after](after.png)"
    sections, _, section_images = naive.Markdown()("guide.md", binary=source.encode(), separate_tables=False, delimiter="`\n`", return_section_images=True)
    assert [section[0] for section in sections] == ["Before", table, "After ![after](after.png)"]
    assert section_images == [None, images["table.png"], images["after.png"]]


def test_naive_chunk_preserves_whole_table_at_zero_chunk_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_visual_model(*args: Any, **kwargs: Any) -> None:
        raise LookupError("No visual model configured for this unit test")

    monkeypatch.setattr(naive, "db_connection", lambda: nullcontext(None))
    monkeypatch.setattr(naive, "get_tenant_default_model_by_type", no_visual_model)
    table = "| Name | Value |\n| - | - |\n| A | 1 |"
    source = f"Before\n{table}\nAfter"
    chunks = naive.chunk(
        "guide.md",
        binary=source.encode(),
        lang="English",
        callback=lambda *args, **kwargs: None,
        tenant_id="unit-tenant",
        parser_config={"chunk_token_num": 0, "delimiter": "`\n`"},
    )
    assert len(chunks) == 4
    assert chunks[0]["doc_type_kwd"] == "table"
    assert chunks[0]["content_with_weight"] == ("<table>\n<thead>\n<tr>\n<th>Name</th>\n<th>Value</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td>A</td>\n<td>1</td>\n</tr>\n</tbody>\n</table>")
    assert [chunk["content_with_weight"] for chunk in chunks[1:]] == ["Before", table, "After"]
    assert all(chunk["content_ltks"] for chunk in chunks)
