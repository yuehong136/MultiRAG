import hashlib
import weakref
from copy import deepcopy
from typing import Any

import numpy as np
import pdfplumber
import pytest
from PIL import Image

from common.app_config import AppConfigError
from core.flow.parser.pdf_chunk_metadata import extract_pdf_positions, reorder_multi_column_bboxes
from deepdoc.parser.pdf_parser import RAGFlowPdfParser
from tests.pdf_bbox_support import make_parser, make_pdf


def bbox_signature(boxes: list[dict[str, Any]]) -> list[Any]:
    return [
        (box["text"], box["layout_type"], box["page_number"], box["top"], box["bottom"], box["position_tag"], box["positions"], box["image"].size, hashlib.sha256(box["image"].tobytes()).hexdigest())
        for box in boxes
    ]


@pytest.mark.parametrize("start,stop", [(0, 53), (3, 53), (49, 53)])
def test_real_pdf_batches_keep_pages_coordinates_crops_and_outlines(monkeypatch: pytest.MonkeyPatch, start: int, stop: int) -> None:
    binary = make_pdf(53)
    signatures = []
    for batch_size in (1, 7, 50, 100):
        monkeypatch.setenv("PDF_PARSER_PAGE_BATCH_SIZE", str(batch_size))
        parser = make_parser()
        progress: list[float] = []
        messages: list[str] = []

        def callback(value: float, msg: str = "") -> None:
            progress.append(value)
            messages.append(msg)

        boxes = parser.parse_into_bboxes(binary, from_page=start, to_page=stop, zoomin=1, callback=callback)
        assert sorted({box["page_number"] for box in boxes}) == list(range(start + 1, stop + 1))
        assert len(boxes) == 2 * (stop - start)
        assert {box["text"] for box in boxes} == {f"{prefix}{page}" for prefix in ("PAGE", "CELL") for page in range(start + 1, stop + 1)}
        for box in boxes:
            assert box["positions"][0][0] == box["page_number"]
            assert RAGFlowPdfParser.extract_positions(box["position_tag"])[0][0] == [box["page_number"] - 1]
            assert extract_pdf_positions(box)[0][0] == box["page_number"]
            assert box["image"].getbbox() is not None  # crops remain readable after window release
        assert progress == sorted(progress) and progress[-1] == 1
        assert any("OCR finished" in message for message in messages)
        assert parser.outlines == [("First", 0, 1), ("After fifty", 0, 51)]
        assert parser.bbox_page_width == 360 + ((start + 1) % 3) * 60
        assert parser.page_images == [] and parser.page_chars == [] and parser.boxes == [] and parser.pdf is None
        signatures.append(bbox_signature(boxes))
        for box in boxes:
            box["image"].close()
    assert all(signature == signatures[0] for signature in signatures)


def test_full_page_images_never_overlap_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDF_PARSER_PAGE_BATCH_SIZE", "7")
    refs: list[weakref.ReferenceType[Image.Image]] = []
    counts: list[int] = []
    original = pdfplumber.page.Page.to_image

    def track(page: Any, **kwargs: Any) -> Any:
        result = original(page, **kwargs)
        refs.append(weakref.ref(result.annotated))
        counts.append(sum(ref() is not None for ref in refs))
        return result

    monkeypatch.setattr(pdfplumber.page.Page, "to_image", track)
    boxes = make_parser().parse_into_bboxes(make_pdf(53), zoomin=1)
    assert max(counts) == 7
    assert all(ref() is None for ref in refs)
    assert len(boxes) == 106


def test_parser_reuse_and_empty_ranges_clear_old_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDF_PARSER_PAGE_BATCH_SIZE", "2")
    parser = make_parser()
    assert parser.parse_into_bboxes(make_pdf(5), zoomin=1)
    boxes = parser.parse_into_bboxes(make_pdf(2), from_page=1, zoomin=1)
    assert {box["page_number"] for box in boxes} == {2}
    assert parser.outlines == [("First", 0, 1)]
    assert parser.parse_into_bboxes(make_pdf(2), from_page=2, zoomin=1) == []
    assert parser.parse_into_bboxes(make_pdf(2), from_page=1, to_page=1, zoomin=1) == []
    assert parser.page_images == [] and parser.boxes == []


def test_unknown_page_count_fails_before_rendering_unbounded_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = make_parser()
    monkeypatch.setattr(parser, "total_page_number", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="Cannot determine PDF page count"):
        parser.parse_into_bboxes(b"invalid")
    assert parser.page_images == []


def test_bad_range_or_batch_size_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        make_parser().parse_into_bboxes(make_pdf(2), from_page=-1)
    monkeypatch.setenv("PDF_PARSER_PAGE_BATCH_SIZE", "0")
    with pytest.raises(AppConfigError, match="page_batch_size"):
        make_parser().parse_into_bboxes(make_pdf(2))


def test_position_tag_handles_multiple_pages_without_changing_coordinates() -> None:
    text = "@@1-2\t3.2\t4.3\t5.4\t6.5##@@3\t1.0\t2.0\t3.0\t4.0##"
    assert RAGFlowPdfParser._offset_position_tag(text, 10) == "@@11-12\t3.2\t4.3\t5.4\t6.5##@@13\t1.0\t2.0\t3.0\t4.0##"


def test_multi_column_reorder_uses_first_selected_page_width_after_release(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = make_parser()
    parser.page_images = [Image.new("RGB", (100, 200))]  # last window is much narrower
    parser.bbox_page_width = 1000.0
    boxes = [{"layout_type": "text", "page_number": 1, "x0": 30, "x1": 130, "top": 0}]
    calls: list[float] = []

    def sort(items: list[dict[str, Any]], width: float) -> list[dict[str, Any]]:
        calls.append(width)
        return items

    monkeypatch.setattr(parser, "sort_X_by_page", sort)
    assert reorder_multi_column_bboxes(parser, boxes) == boxes
    assert calls == [50]
    parser._release_loaded_window()
    assert reorder_multi_column_bboxes(parser, boxes) == boxes and calls == [50, 50]


@pytest.mark.parametrize("with_chars", [True, False])
def test_ocr_allocates_crop_array_only_once_when_needed(monkeypatch: pytest.MonkeyPatch, with_chars: bool) -> None:
    parser = make_parser()
    parser.boxes, parser.lefted_chars = [], []
    parser.mean_height, parser.mean_width = [12], [12]
    image = Image.new("RGB", (300, 300))
    chars = [{"text": "甲", "x0": 25, "x1": 40, "top": top, "bottom": top + 12, "height": 12, "width": 15} for top in (20, 70)] if with_chars else []
    conversions: list[int] = []
    original = np.asarray

    def track(value: Any, *args: Any, **kwargs: Any) -> Any:
        if value is image:
            conversions.append(id(value))
        return original(value, *args, **kwargs)

    monkeypatch.setattr(np, "asarray", track)
    parser._RAGFlowPdfParser__ocr(1, image, chars, ZM=1)
    assert len(conversions) == (0 if with_chars else 1)
    assert len(parser.ocr.crop_inputs) == (0 if with_chars else 2)
    assert len(set(parser.ocr.crop_inputs)) <= 1
    assert len(parser.boxes[0]) == 2


def test_scanned_window_does_not_force_next_text_window_to_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDF_PARSER_PAGE_BATCH_SIZE", "1")
    parser = make_parser()
    original = pdfplumber.page.Page.dedupe_chars

    def chars(page: Any, *args: Any, **kwargs: Any) -> Any:
        if page.page_number == 1:
            return type("ScannedPage", (), {"chars": []})()
        return original(page, *args, **kwargs)

    monkeypatch.setattr(pdfplumber.page.Page, "dedupe_chars", chars)
    boxes = parser.parse_into_bboxes(make_pdf(2), zoomin=1)
    assert {box["text"] for box in boxes if box["page_number"] == 1} == {"SCANNED"}
    assert {box["text"] for box in boxes if box["page_number"] == 2} == {"PAGE2", "CELL2"}
    assert len(parser.ocr.crop_inputs) == 2


def test_multi_page_table_positions_offset_once(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = make_parser()
    parser.__images__(make_pdf(5), 1, page_from=2, page_to=4)
    parser._layouts_rec(1)

    def layouts(*args: Any, **kwargs: Any) -> None:
        parser.boxes = []

    def table(*args: Any, **kwargs: Any) -> Any:
        return [((Image.new("RGB", (20, 20)), ["two-page table"]), [(2, 10, 20, 30, 40), (3, 11, 21, 31, 41)])], []

    monkeypatch.setattr(parser, "_layouts_rec", layouts)
    monkeypatch.setattr(parser, "_extract_table_figure", table)
    local = parser._parse_loaded_window_into_bboxes(1)
    local_before = deepcopy(local[0]["positions"])
    boxes = parser._to_global_boxes(local, 100)
    assert local_before == [[1, 10, 20, 30, 40], [2, 11, 21, 31, 41]]
    assert boxes[0]["page_number"] == 3
    assert boxes[0]["positions"] == [[3, 10, 20, 30, 40], [4, 11, 21, 31, 41]]
    assert boxes[0]["top"] == 130
    assert "@@3\t" in boxes[0]["position_tag"] and "@@4\t" in boxes[0]["position_tag"]
    parser._release_loaded_window()


def test_rotated_table_ocr_keeps_local_page_until_final_offset() -> None:
    parser = make_parser()
    parser.page_from = 10
    parser.page_cum_height = [0, 200, 400]
    parser.boxes = [{"page_number": 2, "layout_type": "table", "x0": 10, "x1": 30, "top": 220, "bottom": 240, "text": "old"}]
    parser.table_rotations = {0: {"best_angle": 180}}
    parser.rotated_table_imgs = {0: Image.new("RGB", (40, 40))}
    parser.ocr = lambda image: [([[0, 0], [10, 0], [10, 10], [0, 10]], ("new", 0.99))]
    layout = {"x0": 10, "x1": 50, "top": 20, "bottom": 60}
    parser._ocr_rotated_tables(1, [{"table_index": 0, "page": 1, "layout": layout, "coords": (10, 20, 50, 60)}], [], [0, 0, 1])
    assert len(parser.boxes) == 1
    assert parser.boxes[0]["text"] == "new"
    assert parser.boxes[0]["page_number"] == 2
    assert parser.boxes[0]["top"] == 250
    parser._release_loaded_window()


def test_text_spanning_two_pages_keeps_both_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = make_parser()
    parser.__images__(make_pdf(5), 1, page_from=2, page_to=4)
    first_height = parser.page_images[0].height

    def layouts(*args: Any, **kwargs: Any) -> None:
        parser.boxes = [{"page_number": 1, "x0": 20, "x1": 80, "top": 30, "bottom": first_height + 40, "text": "two-page paragraph", "layout_type": "text"}]

    monkeypatch.setattr(parser, "_layouts_rec", layouts)
    monkeypatch.setattr(parser, "_extract_table_figure", lambda *args: ([], []))
    local = parser._parse_loaded_window_into_bboxes(1)
    assert [position[0] for position in local[0]["positions"]] == [1, 2]
    boxes = parser._to_global_boxes(local)
    assert boxes[0]["page_number"] == 3
    assert boxes[0]["positions"] == [[3, 20.0, 80.0, 30.0, first_height], [4, 20.0, 80.0, 0, 40.0]]
    assert boxes[0]["position_tag"].startswith("@@3-4\t")
    parser._release_loaded_window()
