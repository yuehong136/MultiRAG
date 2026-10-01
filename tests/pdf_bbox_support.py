"""Real PDF/rendering fixtures; only OCR/layout/table inference and merging are fake."""

from io import BytesIO
from typing import Any

import numpy as np
from PIL import Image
from pypdf import PdfWriter
from reportlab.pdfgen import canvas

from deepdoc.parser.pdf_parser import RAGFlowPdfParser
from deepdoc.vision.layout_recognizer import LayoutRecognizer
from deepdoc.vision.ocr import OCR


def make_pdf(pages: int, *, mixed_sizes: bool = True) -> bytes:
    stream = BytesIO()
    pdf = canvas.Canvas(stream)
    for page in range(1, pages + 1):
        width, height = (360 + (page % 3) * 60, 480 + (page % 4) * 40) if mixed_sizes else (612, 792)
        pdf.setPageSize((width, height))
        pdf.drawString(20, height - 30, f"PAGE{page}")
        pdf.drawString(20, height - 80, f"CELL{page}")
        pdf.showPage()
    pdf.save()
    writer = PdfWriter()
    writer.append(BytesIO(stream.getvalue()))
    writer.add_outline_item("First", 0)
    if pages > 50:
        writer.add_outline_item("After fifty", 50)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class FakeOCR:
    def __init__(self, zoom: float) -> None:
        self.zoom = zoom
        self.crop_inputs: list[int] = []

    def detect(self, img: np.ndarray, device_id: int | None = None) -> list[Any]:
        return [(np.array([[20, top], [150, top], [150, top + 25], [20, top + 25]], dtype=np.float32) * self.zoom, ("", 0)) for top in (15, 65)]

    def get_rotate_crop_image(self, img: np.ndarray, quad: np.ndarray) -> np.ndarray:
        self.crop_inputs.append(id(img))
        return OCR.get_rotate_crop_image(self, img, quad)

    def recognize_batch(self, images: list[np.ndarray], device_id: int | None = None) -> list[str]:
        return ["SCANNED" for _ in images]


class FakeLayoutClient:
    def __init__(self, zoom: float) -> None:
        self.zoom = zoom

    def predict(self, images: list[Image.Image]) -> list[list[dict[str, Any]]]:
        return [[{"type": kind, "score": 0.99, "bbox": [value * self.zoom for value in bbox]} for kind, bbox in [("text", (20, 15, 150, 40)), ("table", (20, 65, 150, 90))]] for _ in images]


class FakeTable:
    @staticmethod
    def construct_table(boxes: list[dict[str, Any]], **kwargs: Any) -> str:
        return "\n".join(box["text"] for box in boxes)


def make_parser(zoom: float = 1.0) -> RAGFlowPdfParser:
    parser = RAGFlowPdfParser.__new__(RAGFlowPdfParser)
    parser.parallel_limiter = None
    parser.page_from = 0
    parser.column_num = 1
    parser.ocr = FakeOCR(zoom)
    layouter = LayoutRecognizer.__new__(LayoutRecognizer)
    layouter.client = FakeLayoutClient(zoom)
    layouter.garbage_layouts = ["footer", "header", "reference"]
    parser.layouter = layouter
    parser.tbl_det = FakeTable()

    def table_job(*args: Any, **kwargs: Any) -> None:
        parser.tb_cpns = []

    def no_merge(*args: Any, **kwargs: Any) -> None:
        pass

    parser._table_transformer_job = table_job
    parser._text_merge = no_merge
    parser._concat_downward = no_merge
    parser._naive_vertical_merge = no_merge
    return parser
