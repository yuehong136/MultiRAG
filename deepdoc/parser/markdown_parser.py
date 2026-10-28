#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import re
from typing import Any

from markdown import markdown


def _get_fence_marker(line: str) -> tuple[str, int] | None:
    match = re.match(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?:.*)$", line)
    if not match:
        return None
    fence = match.group("fence")
    return fence[0], len(fence)


def _is_closing_fence(line: str, fence_char: str, fence_len: int) -> bool:
    return re.fullmatch(r"[ \t]{0,3}" + re.escape(fence_char) + "{" + str(fence_len) + r",}[ \t\r]*", line) is not None


def _fenced_code_ranges(text: str) -> list[tuple[int, int]]:
    """Share fence boundaries between table preprocessing and delimiter extraction."""
    ranges = []
    marker = None
    start = offset = 0
    for line in text.split("\n"):
        if marker is None:
            marker = _get_fence_marker(line)
            if marker:
                start = offset
        elif _is_closing_fence(line, *marker):
            ranges.append((start, offset + len(line)))
            marker = None
        offset += len(line) + 1
    if marker:
        ranges.append((start, len(text)))
    return ranges


class RAGFlowMarkdownParser:
    def __init__(self, chunk_token_num=128):
        self.chunk_token_num = int(chunk_token_num)

    def extract_tables_and_remainder(self, markdown_text: str, separate_tables: bool = True) -> tuple[str, list[str]]:
        # Run the existing table grammar only outside fences. In particular,
        # neither HTML attribute normalization nor table removal may touch code.
        pieces = []
        tables = []
        last_end = 0
        for start, end in _fenced_code_ranges(markdown_text):
            remainder, extracted = self._extract_unfenced_tables_and_remainder(markdown_text[last_end:start], separate_tables)
            pieces.extend((remainder, markdown_text[start:end]))
            tables.extend(extracted)
            last_end = end
        remainder, extracted = self._extract_unfenced_tables_and_remainder(markdown_text[last_end:], separate_tables)
        pieces.append(remainder)
        tables.extend(extracted)
        return "".join(pieces), tables

    def _extract_unfenced_tables_and_remainder(self, markdown_text: str, separate_tables: bool) -> tuple[str, list[str]]:
        tables = []
        working_text = markdown_text

        def replace_tables_with_rendered_html(pattern: re.Pattern[str], table_list: list[str], render: bool = True) -> str:
            new_text = ""
            last_end = 0
            for match in pattern.finditer(working_text):
                raw_table = match.group()
                table_list.append(raw_table)
                if separate_tables:
                    # Skip this match (i.e., remove it)
                    new_text += working_text[last_end : match.start()] + "\n\n"
                else:
                    # Replace with rendered HTML
                    html_table = markdown(raw_table, extensions=["markdown.extensions.tables"]) if render else raw_table
                    new_text += working_text[last_end : match.start()] + html_table + "\n\n"
                last_end = match.end()
            new_text += working_text[last_end:]
            return new_text

        if "|" in markdown_text:  # for optimize performance
            # Standard Markdown table
            border_table_pattern = re.compile(
                r"""
                (?:\n|^)
                (?:\|.*?\|.*?\|.*?\n)
                (?:\|(?:\s*[:-]+[-| :]*\s*)\|.*?\n)
                (?:\|.*?\|.*?\|.*?\n)+
            """,
                re.VERBOSE,
            )
            working_text = replace_tables_with_rendered_html(border_table_pattern, tables, render=separate_tables)

            # Borderless Markdown table
            no_border_table_pattern = re.compile(
                r"""
                (?:\n|^)
                (?:\S.*?\|.*?\n)
                (?:(?:\s*[:-]+[-| :]*\s*).*?\n)
                (?:\S.*?\|.*?\n)+
                """,
                re.VERBOSE,
            )
            working_text = replace_tables_with_rendered_html(no_border_table_pattern, tables, render=separate_tables)

        # Replace any TAGS e.g. <table ...> to <table>
        TAGS = ["table", "td", "tr", "th", "tbody", "thead", "div"]
        table_with_attributes_pattern = re.compile(rf"<(?:{'|'.join(TAGS)})[^>]*>", re.IGNORECASE)

        def replace_tag(m: re.Match[str]) -> str:
            tag_name = re.match(r"<(\w+)", m.group()).group(1)
            return f"<{tag_name}>"

        working_text = re.sub(table_with_attributes_pattern, replace_tag, working_text)

        if "<table>" in working_text.lower():  # for optimize performance
            # HTML table extraction - handle possible html/body wrapper tags
            html_table_pattern = re.compile(
                r"""
            (?:\n|^)
            \s*
            (?:
                # case1: <html><body><table>...</table></body></html>
                (?:<html[^>]*>\s*<body[^>]*>\s*<table[^>]*>.*?</table>\s*</body>\s*</html>)
                |
                # case2: <body><table>...</table></body>
                (?:<body[^>]*>\s*<table[^>]*>.*?</table>\s*</body>)
                |
                # case3: only<table>...</table>
                (?:<table[^>]*>.*?</table>)
            )
            \s*
            (?=\n|$)
            """,
                re.VERBOSE | re.DOTALL | re.IGNORECASE,
            )

            def replace_html_tables() -> None:
                nonlocal working_text
                new_text = ""
                last_end = 0
                for match in html_table_pattern.finditer(working_text):
                    raw_table = match.group()
                    tables.append(raw_table)
                    if separate_tables:
                        new_text += working_text[last_end : match.start()] + "\n\n"
                    else:
                        new_text += working_text[last_end : match.start()] + raw_table + "\n\n"
                    last_end = match.end()
                new_text += working_text[last_end:]
                working_text = new_text

            replace_html_tables()

        return working_text, tables


class MarkdownElementExtractor:
    def __init__(self, markdown_content: str) -> None:
        self.markdown_content = markdown_content
        self.lines = markdown_content.split("\n")

    def get_delimiters(self, delimiters: str) -> str:
        toks = re.findall(r"`([^`]+)`", delimiters)
        toks = sorted(set(toks), key=lambda x: -len(x))
        return "|".join(re.escape(t) for t in toks if t)

    def _extract_delimited_elements(self, text: str, delimiters: str, include_meta: bool) -> list[str] | list[dict[str, Any]]:
        sections = []
        ranges = _fenced_code_ranges(text)
        protected_idx = 0
        last_end = 0

        def append_section(start: int, end: int) -> None:
            part = text[start:end]
            if not part.strip():
                return
            if include_meta:
                sections.append({"content": part.strip(), "start_line": text.count("\n", 0, start), "end_line": text.count("\n", 0, end)})
            else:
                sections.append(part.strip())

        for match in re.finditer(delimiters, text):
            while protected_idx < len(ranges) and ranges[protected_idx][1] <= match.start():
                protected_idx += 1
            if protected_idx < len(ranges) and match.end() > ranges[protected_idx][0]:
                continue
            append_section(last_end, match.start())
            last_end = match.end()
        append_section(last_end, len(text))
        return sections

    def extract_elements(self, delimiter: str | None = None, include_meta: bool = False) -> list[str] | list[dict[str, Any]]:
        """Extract individual elements (headers, code blocks, lists, etc.)"""
        sections = []

        i = 0
        dels = ""
        if delimiter:
            dels = self.get_delimiters(delimiter)
        if len(dels) > 0:
            text = "\n".join(self.lines)
            return self._extract_delimited_elements(text, dels, include_meta)
        while i < len(self.lines):
            line = self.lines[i]

            if re.match(r"^#{1,6}\s+.*$", line):
                # header
                element = self._extract_header(i)
                sections.append(element if include_meta else element["content"])
                i = element["end_line"] + 1
            elif _get_fence_marker(line):
                # code block
                element = self._extract_code_block(i)
                sections.append(element if include_meta else element["content"])
                i = element["end_line"] + 1
            elif re.match(r"^\s*[-*+]\s+.*$", line) or re.match(r"^\s*\d+\.\s+.*$", line):
                # list block
                element = self._extract_list_block(i)
                sections.append(element if include_meta else element["content"])
                i = element["end_line"] + 1
            elif line.strip().startswith(">"):
                # blockquote
                element = self._extract_blockquote(i)
                sections.append(element if include_meta else element["content"])
                i = element["end_line"] + 1
            elif line.strip():
                # text block (paragraphs and inline elements until next block element)
                element = self._extract_text_block(i)
                sections.append(element if include_meta else element["content"])
                i = element["end_line"] + 1
            else:
                i += 1

        if include_meta:
            sections = [section for section in sections if section["content"].strip()]
        else:
            sections = [section for section in sections if section.strip()]
        return sections

    def _extract_header(self, start_pos):
        return {
            "type": "header",
            "content": self.lines[start_pos],
            "start_line": start_pos,
            "end_line": start_pos,
        }

    def _extract_code_block(self, start_pos: int) -> dict[str, Any]:
        end_pos = start_pos
        content_lines = [self.lines[start_pos]]
        marker = _get_fence_marker(self.lines[start_pos])
        assert marker is not None

        # Find the end of the code block
        for i in range(start_pos + 1, len(self.lines)):
            content_lines.append(self.lines[i])
            end_pos = i
            if _is_closing_fence(self.lines[i], *marker):
                break

        return {
            "type": "code_block",
            "content": "\n".join(content_lines),
            "start_line": start_pos,
            "end_line": end_pos,
        }

    def _extract_list_block(self, start_pos):
        end_pos = start_pos
        content_lines = []

        i = start_pos
        while i < len(self.lines):
            line = self.lines[i]
            # check if this line is a list item or continuation of a list
            if (
                re.match(r"^\s*[-*+]\s+.*$", line)
                or re.match(r"^\s*\d+\.\s+.*$", line)
                or (i > start_pos and not line.strip())
                or (i > start_pos and re.match(r"^\s{2,}[-*+]\s+.*$", line))
                or (i > start_pos and re.match(r"^\s{2,}\d+\.\s+.*$", line))
                or (i > start_pos and re.match(r"^\s+\w+.*$", line))
            ):
                content_lines.append(line)
                end_pos = i
                i += 1
            else:
                break

        return {
            "type": "list_block",
            "content": "\n".join(content_lines),
            "start_line": start_pos,
            "end_line": end_pos,
        }

    def _extract_blockquote(self, start_pos):
        end_pos = start_pos
        content_lines = []

        i = start_pos
        while i < len(self.lines):
            line = self.lines[i]
            if line.strip().startswith(">") or (i > start_pos and not line.strip()):
                content_lines.append(line)
                end_pos = i
                i += 1
            else:
                break

        return {
            "type": "blockquote",
            "content": "\n".join(content_lines),
            "start_line": start_pos,
            "end_line": end_pos,
        }

    def _extract_text_block(self, start_pos: int) -> dict[str, Any]:
        """Extract a text block (paragraphs, inline elements) until next block element"""
        end_pos = start_pos
        content_lines = [self.lines[start_pos]]

        i = start_pos + 1
        while i < len(self.lines):
            line = self.lines[i]
            # stop if we encounter a block element
            if re.match(r"^#{1,6}\s+.*$", line) or _get_fence_marker(line) or re.match(r"^\s*[-*+]\s+.*$", line) or re.match(r"^\s*\d+\.\s+.*$", line) or line.strip().startswith(">"):
                break
            elif not line.strip():
                # check if the next line is a block element
                if i + 1 < len(self.lines) and (
                    re.match(r"^#{1,6}\s+.*$", self.lines[i + 1])
                    or _get_fence_marker(self.lines[i + 1])
                    or re.match(r"^\s*[-*+]\s+.*$", self.lines[i + 1])
                    or re.match(r"^\s*\d+\.\s+.*$", self.lines[i + 1])
                    or self.lines[i + 1].strip().startswith(">")
                ):
                    break
                else:
                    content_lines.append(line)
                    end_pos = i
                    i += 1
            else:
                content_lines.append(line)
                end_pos = i
                i += 1

        return {
            "type": "text_block",
            "content": "\n".join(content_lines),
            "start_line": start_pos,
            "end_line": end_pos,
        }
