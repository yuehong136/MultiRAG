import asyncio
from types import SimpleNamespace

from core.flow.parser.pdf_chunk_metadata import (
    PDF_POSITIONS_KEY,
    build_pdf_position_fields,
    extract_pdf_positions,
    finalize_pdf_chunk,
    merge_pdf_positions,
    normalize_pdf_items_metadata,
    restore_pdf_text_previews,
)


def test_pdf_positions_normalize_zero_based_pages_and_finalize_fields():
    item = {
        "text": "alpha",
        "page_number": 0,
        "x0": 10.6,
        "x1": 30.2,
        "top": 12.9,
        "bottom": 25.1,
    }

    assert extract_pdf_positions(item) == [[1, 10.6, 30.2, 12.9, 25.1]]

    chunk = finalize_pdf_chunk({PDF_POSITIONS_KEY: extract_pdf_positions(item), "text": "alpha"})

    assert PDF_POSITIONS_KEY not in chunk
    assert chunk["position_int"] == [(1, 10, 30, 12, 25)]
    assert chunk["page_num_int"] == [1]
    assert chunk["top_int"] == [12]


def test_pdf_positions_restore_one_based_page_from_position_tag():
    item = {
        "text": "alpha @@2\t10.6\t30.2\t12.9\t25.1##",
        "position_tag": "@@2\t10.6\t30.2\t12.9\t25.1##",
    }

    assert extract_pdf_positions(item) == [[2, 10.6, 30.2, 12.9, 25.1]]


def test_pdf_positions_do_not_double_offset_when_positions_and_tag_coexist():
    item = {
        "text": "alpha @@2\t10.6\t30.2\t12.9\t25.1##",
        "page_number": 2,
        "positions": [[2, 10.6, 30.2, 12.9, 25.1]],
        "position_tag": "@@2\t10.6\t30.2\t12.9\t25.1##",
    }

    assert extract_pdf_positions(item) == [[2, 10.6, 30.2, 12.9, 25.1]]


def test_pdf_positions_merge_deduplicates_and_sorts():
    merged = merge_pdf_positions(
        [
            {PDF_POSITIONS_KEY: [[2, 20, 30, 50, 60], [1, 10, 20, 40, 50]]},
            [[1, 10, 20, 40, 50], [1, 5, 15, 10, 20]],
        ]
    )

    assert merged == [
        [1, 5, 15, 10, 20],
        [1, 10, 20, 40, 50],
        [2, 20, 30, 50, 60],
    ]
    assert build_pdf_position_fields(merged)["page_num_int"] == [1, 1, 2]


def test_normalize_pdf_items_uses_internal_key_without_persistent_leak():
    items = [
        {"text": "alpha", "position_int": [[0, 1, 2, 3, 4]]},
        {"text": "beta"},
    ]

    normalize_pdf_items_metadata(items)

    assert items[0][PDF_POSITIONS_KEY] == [[1, 1.0, 2.0, 3.0, 4.0]]
    assert PDF_POSITIONS_KEY not in items[1]
    assert PDF_POSITIONS_KEY not in finalize_pdf_chunk(items[0])


def test_restore_pdf_text_previews_safely_skips_without_positions_or_blob():
    canvas = SimpleNamespace(_doc_id=None, _tenant_id="tenant-1")
    upstream = SimpleNamespace(name="sample.pdf", file=None)

    asyncio.run(restore_pdf_text_previews([], upstream, canvas))
    asyncio.run(restore_pdf_text_previews([{"text": "alpha"}], upstream, canvas))

    chunks = [{"text": "alpha", PDF_POSITIONS_KEY: [[1, 1, 2, 3, 4]]}]
    asyncio.run(restore_pdf_text_previews(chunks, upstream, canvas))

    assert chunks == [{"text": "alpha", PDF_POSITIONS_KEY: [[1, 1, 2, 3, 4]]}]
