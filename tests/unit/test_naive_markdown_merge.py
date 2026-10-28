"""Markdown source regressions through the active naive parsing/tokenization chain."""

from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

import pytest
from PIL import Image


@pytest.fixture
def parse_markdown(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[dict[str, Any]]]:
    from core.app import naive

    def no_visual_model(*args: Any, **kwargs: Any) -> None:
        raise LookupError("No visual model configured for this unit test")

    monkeypatch.setattr(naive, "db_connection", lambda: nullcontext(None))
    monkeypatch.setattr(naive, "get_tenant_default_model_by_type", no_visual_model)

    def parse(source: str, limit: int = 0, overlap: int = 0, delimiter: str = "\n") -> list[dict[str, Any]]:
        return naive.chunk(
            "guide.md",
            binary=source.encode(),
            lang="English",
            callback=lambda *args, **kwargs: None,
            tenant_id="unit-tenant",
            parser_config={"chunk_token_num": limit, "delimiter": delimiter, "overlapped_percent": overlap},
        )

    return parse


@pytest.mark.parametrize("level", range(1, 7))
@pytest.mark.parametrize("delimiter", ["\n", "`\n`"])
def test_short_heading_stays_with_following_body(parse_markdown: Callable[..., Any], level: int, delimiter: str) -> None:
    header = "#" * level + " Quick Travel"
    body = ("Take the morning train to the station. " * 12).strip()
    chunks = parse_markdown(f"{header}\n{body}\n## Next\nNext destination.", limit=8, delimiter=delimiter)
    assert [chunk["content_with_weight"] for chunk in chunks] == [f"{header}\n{body}", "## Next\nNext destination."]
    assert chunks[0]["docnm_kwd"] == "guide.md"
    assert chunks[0]["position_int"] == [(1, 0, 0, 0, 0)]
    assert chunks[1]["position_int"] == [(2, 1, 1, 1, 1)]
    assert chunks[0]["content_ltks"]


@pytest.mark.parametrize("body", ["First paragraph.", ("First paragraph. " * 80).strip()])
def test_heading_with_body_does_not_keep_absorbing_sections(parse_markdown: Callable[..., Any], body: str) -> None:
    following = ("Second paragraph. " * 80).strip()
    chunks = parse_markdown(f"## Overview\n{body}\n{following}", limit=8, delimiter="`\n`")
    assert [chunk["content_with_weight"] for chunk in chunks] == [f"## Overview\n{body}", following]


@pytest.mark.parametrize("header", ["# " + "word " * 60, "####### Heading", "#No-space"])
def test_non_short_or_non_heading_obeys_limit(parse_markdown: Callable[..., Any], header: str) -> None:
    chunks = parse_markdown(header + "\nBody text", limit=0, delimiter="`\n`")
    assert [chunk["content_with_weight"] for chunk in chunks] == [header.strip(), "Body text"]


def test_consecutive_headings_stay_with_body(parse_markdown: Callable[..., Any]) -> None:
    body = ("Paragraph after nested headings. " * 60).strip()
    source = "# Guide\n## Travel\n" + body + "\n## Next\nFinal paragraph."
    chunks = parse_markdown(source, limit=0, delimiter="`\n`")
    assert [chunk["content_with_weight"] for chunk in chunks] == ["# Guide\n## Travel\n" + body, "## Next\nFinal paragraph."]


def test_header_threshold_uses_real_tokenizer() -> None:
    from common.token_utils import num_tokens_from_string
    from core.app.naive import _is_short_header

    for text in ("# Short", "## 快速旅行", "###### API: /v1/users"):
        tokens = num_tokens_from_string(text)
        assert tokens > 0
        assert _is_short_header(text, tokens + 1)
        assert not _is_short_header(text, tokens)
    assert not _is_short_header("# Heading\nBody")
    assert not _is_short_header("#\nBody")
    assert not _is_short_header("   ")


def test_overlap_retains_existing_suffix_contract(parse_markdown: Callable[..., Any]) -> None:
    header = "## Overview"
    body = ("The first paragraph has enough content. " * 20).strip()
    following = ("The second paragraph has enough content. " * 20).strip()
    merged = header + "\n" + body
    suffix = merged[-int(len(merged) * 0.2) :]
    chunks = parse_markdown(merged + "\n" + following, limit=8, overlap=20, delimiter="`\n`")
    assert [chunk["content_with_weight"] for chunk in chunks] == [merged, suffix + "\n" + following]


def test_empty_and_trailing_header(parse_markdown: Callable[..., Any]) -> None:
    assert parse_markdown("\n  \n") == []
    assert [chunk["content_with_weight"] for chunk in parse_markdown("## Last heading")] == ["## Last heading"]


def test_rtl_heading_and_body_remain_normalized(parse_markdown: Callable[..., Any]) -> None:
    from common.text_utils import normalize_arabic_presentation_forms

    source = "## ﺍﻟﻌﺮﺑﻴﺔ\nﻣﺮﺣﺒﺎ ﺑﺎﻟﻌﺎﻟﻢ"
    chunks = parse_markdown(source)
    assert [chunk["content_with_weight"] for chunk in chunks] == [normalize_arabic_presentation_forms(source)]


def test_images_keep_section_order_and_association(parse_markdown: Callable[..., Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from core.app import naive

    images = {"red.png": Image.new("RGB", (2, 2), "red"), "blue.png": Image.new("RGB", (2, 2), "blue")}

    def load_images(self: Any, urls: list[str], cache: Any = None) -> tuple[list[Image.Image], Any]:
        return [images[url] for url in urls], cache

    monkeypatch.setattr(naive.Markdown, "load_images_from_urls", load_images)
    source = "## Pictures\n![red](red.png)\n![blue](blue.png)\n## Text\nFollowing content"
    limit = sum(naive.num_tokens_from_string(line) for line in source.splitlines()[:3])
    chunks = parse_markdown(source, limit=limit, delimiter="`\n`")
    assert len(chunks) == 2
    assert chunks[0]["content_with_weight"] == "## Pictures\n![red](red.png)\n![blue](blue.png)"
    image = chunks[0]["image"]
    assert image.getpixel((0, 0)) == (255, 0, 0)
    assert image.getpixel((0, image.height - 1)) == (0, 0, 255)
    assert chunks[1]["image"] is None
