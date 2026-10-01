"""Compare real PDF rendering/bbox crops with isolated inference boundaries.

Run each batch size in a fresh process, for example:
uv run --no-sync python tests/manual/pdf_bbox_batch_memory.py --pages 121 --batch-size 7
"""

import argparse
import hashlib
import json
import sys
import time
import weakref
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pdfplumber
import psutil
from PIL import Image

from deepdoc.parser.pdf_parser import RAGFlowPdfParser
from tests.pdf_bbox_support import make_parser, make_pdf


def main() -> None:
    import os
    import resource

    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=121)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--from-page", type=int, default=0)
    parser.add_argument("--zoom", type=float, default=1)
    parser.add_argument("--real-models", action="store_true")
    args = parser.parse_args()
    os.environ["PDF_PARSER_PAGE_BATCH_SIZE"] = str(args.batch_size)
    blob = make_pdf(args.pages, mixed_sizes=False)
    process = psutil.Process()
    baseline_rss = process.memory_info().rss
    images: list[weakref.ReferenceType[Image.Image]] = []
    max_images = 0
    max_raster_bytes = 0
    original = pdfplumber.page.Page.to_image

    def track(page: Any, **kwargs: Any) -> Any:
        nonlocal max_images, max_raster_bytes
        result = original(page, **kwargs)
        images.append(weakref.ref(result.annotated))
        active = [image() for image in images if image() is not None]
        max_images = max(max_images, len(active))
        max_raster_bytes = max(max_raster_bytes, sum(image.width * image.height * len(image.getbands()) for image in active))
        return result

    pdfplumber.page.Page.to_image = track
    reader = RAGFlowPdfParser() if args.real_models else make_parser(args.zoom)
    started = time.monotonic()
    boxes = reader.parse_into_bboxes(blob, zoomin=args.zoom, from_page=args.from_page)
    elapsed = time.monotonic() - started
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_rss_bytes = peak_rss if sys.platform == "darwin" else peak_rss * 1024
    page_numbers = sorted({box["page_number"] for box in boxes})
    assert page_numbers == list(range(args.from_page + 1, args.pages + 1))
    assert all(image() is None for image in images)
    signature = [
        (
            box["text"],
            box["layout_type"],
            box["page_number"],
            box["top"],
            box["bottom"],
            box["position_tag"],
            box["positions"],
            hashlib.sha256(box["image"].tobytes()).hexdigest() if box.get("image") is not None else None,
        )
        for box in boxes
    ]
    print(
        json.dumps(
            {
                "pages": args.pages,
                "from_page": args.from_page,
                "batch_size": args.batch_size,
                "resolution_dpi": 72 * args.zoom,
                "raster_pixels": [int(612 * args.zoom), int(792 * args.zoom)],
                "bbox_count": len(boxes),
                "last_page": page_numbers[-1],
                "max_live_full_page_images": max_images,
                "max_full_page_raster_mib": round(max_raster_bytes / 1024**2, 2),
                "baseline_rss_mib": round(baseline_rss / 1024**2, 2),
                "peak_rss_mib": round(peak_rss_bytes / 1024**2, 2),
                "elapsed_seconds": round(elapsed, 2),
                "result_sha256": hashlib.sha256(json.dumps(signature).encode()).hexdigest(),
                "inference": "local OCR/layout/table models and merge" if args.real_models else "fake OCR/layout/table inference and merge; real PDF rendering, text extraction, bbox and crops",
            }
        )
    )


if __name__ == "__main__":
    main()
