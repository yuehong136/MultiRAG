"""Page decoration and unsupported MinerU blocks must never duplicate body content."""

import json
import logging
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from deepdoc.parser.mineru_parser import MinerUParser
from tests.pdf_bbox_support import make_pdf


def block(kind: str, text: str = "", **fields: Any) -> dict[str, Any]:
    return {"type": kind, "text": text, "page_idx": 0, "bbox": [100, 200, 400, 300], **fields}


@pytest.mark.parametrize("parse_method", [None, "raw", "manual", "pipeline", "paper"])
def test_page_decoration_keeps_body_order_and_positions(parse_method: str | None) -> None:
    parser = MinerUParser()
    body = [block("text", "打开和关闭"), block("text", "车辆装备"), block("text", "车辆钥匙"), block("text", "概述"), block("text", "安全提示", page_idx=1)]
    outputs = [
        body[0],
        body[1],
        block("header", "打开和关闭"),
        body[2],
        block("page_number", "77"),
        block("footer", "Online Edition for Part no. 01405A89DE4 - VI/23"),
        body[3],
        block("header", "概述", page_idx=1),
        block("discarded", "Dropped OCR block"),
        body[4],
    ]
    actual = parser._transfer_to_sections(outputs, parse_method)
    assert actual == parser._transfer_to_sections(body, parse_method)
    assert len(actual) == len(body)
    assert actual[-1][0].startswith("安全提示")
    assert "@@2\t100.0\t400.0\t200.0\t300.0##" in "".join(actual[-1])


@pytest.mark.parametrize("kind", ["sidebar", "chart", "header", "footer", "page_number", "discarded", None])
@pytest.mark.parametrize("placement", ["first", "between", "only"])
def test_unsupported_blocks_never_reuse_a_previous_section(kind: str | None, placement: str, caplog: pytest.LogCaptureFixture) -> None:
    parser = MinerUParser()
    unsupported = {"type": kind, "text": "Must not become a chunk"} if kind else {"text": "Missing type"}
    body = [block("text", "Primary content"), block("text", "Next content")]
    outputs = [unsupported, *body] if placement == "first" else [body[0], unsupported, body[1]] if placement == "between" else [unsupported]
    with caplog.at_level(logging.DEBUG, logger=parser.logger.name):
        actual = parser._transfer_to_sections(outputs, "raw")
    assert actual == parser._transfer_to_sections(body if placement != "only" else [], "raw")
    if kind in {"sidebar", "chart", None}:
        assert f"Skip unsupported section type={kind}" in caplog.text


@pytest.mark.parametrize("parse_method", ["raw", "manual", "pipeline", "paper"])
def test_supported_blocks_keep_vlm_description_and_tuple_contract(parse_method: str) -> None:
    outputs = [
        block("text", "Body"),
        block("table", table_body="<table><tr><td>Cell</td></tr></table>", table_caption=["Table 1"], table_footnote=["Source"]),
        block("image", image_caption=["Figure 2"], image_footnote=["Footnote"], vlm_description="VLM description"),
        block("equation", "x < y"),
        block("code", code_body="print(1)", code_caption=["Example"]),
        block("list", list_items=["First item", "Second item"]),
    ]
    texts = ["Body", "Cell\n\nTable 1Source", "Figure 2\nFootnote\nVLM description", "x < y", "print(1)Example", "First item\nSecond item"]
    parser = MinerUParser()
    tag = "@@1\t100.0\t400.0\t200.0\t300.0##"
    expected = []
    for output, text in zip(outputs, texts, strict=True):
        if parse_method in {"manual", "pipeline"}:
            expected.append((text, output["type"], tag))
        elif parse_method == "paper":
            expected.append((text + tag, output["type"]))
        else:
            expected.append((text, tag))
    interspersed = [item for output in outputs for item in (output, {"type": "header"}, {"type": "future-block"})]
    assert parser._transfer_to_sections(interspersed, parse_method) == expected


def test_parse_pdf_zip_readback_excludes_decoration_and_keeps_scaled_positions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run real rendering, ZIP discovery and section conversion across the HTTP boundary."""
    outputs = [
        block("header", "Repeated title"),
        block("text", "Actual body"),
        block("sidebar", "Unsupported sidebar"),
        {"text": "Missing type"},
        block("page_number", "1"),
        block("text", "Next body"),
        block("footer", "Repeated footer"),
    ]
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("report/vlm/report_content_list.json", json.dumps(outputs))
    archive.seek(0)
    response = MagicMock()
    response.__enter__.return_value = response
    response.headers = {"Content-Type": "application/zip"}
    response.raw = archive
    monkeypatch.setattr("deepdoc.parser.mineru_parser.requests.post", lambda **kwargs: response)
    original_mkdtemp = tempfile.mkdtemp
    scratch: list[Path] = []

    def allocate(*args: Any, **kwargs: Any) -> str:
        path = original_mkdtemp(*args, **kwargs)
        scratch.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", allocate)
    parser = MinerUParser(mineru_api="http://mineru.invalid")
    destination = tmp_path / "output"
    try:
        sections, tables = parser.parse_pdf("report.pdf", make_pdf(1), output_dir=str(destination), backend="vlm-http-client", parse_method="pipeline")
        assert sections == [("Actual body", "text", "@@1\t42.0\t168.0\t104.0\t156.0##"), ("Next body", "text", "@@1\t42.0\t168.0\t104.0\t156.0##")]
        assert tables == []
        extracted = [path for path in scratch if path.parent == destination]
        assert len(extracted) == 1
        assert json.loads((extracted[0] / "vlm/report_content_list.json").read_text()) == outputs
        binary_dirs = [path for path in scratch if path.name.startswith("mineru_bin_pdf_")]
        assert binary_dirs and all(not path.exists() for path in binary_dirs)
    finally:
        for image in parser.page_images:
            image.close()
