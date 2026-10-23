"""Parser domains use the public species attribute across real constructors."""

from unittest.mock import Mock

import pytest
from PIL import Image

from core.app import laws, manual, paper
from deepdoc.parser import pdf_parser
from deepdoc.parser.pdf_parser import RAGFlowPdfParser


@pytest.fixture
def recognizers(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    boundaries = {name: Mock(name=name) for name in ("OCR", "LayoutRecognizer", "AscendLayoutRecognizer", "TableStructureRecognizer")}
    for name, boundary in boundaries.items():
        monkeypatch.setattr(pdf_parser, name, boundary)
    monkeypatch.setattr(pdf_parser.xgb, "Booster", Mock())
    monkeypatch.setattr(pdf_parser, "snapshot_download", Mock(side_effect=AssertionError("Constructors must not download test models")))
    monkeypatch.setattr(pdf_parser.settings, "PARALLEL_DEVICES", 1)
    return boundaries


@pytest.mark.parametrize("engine", ["onnx", "ascend"])
@pytest.mark.parametrize(
    ("parser_type", "domain"),
    [(RAGFlowPdfParser, "layout"), (laws.Pdf, "layout.laws"), (manual.Pdf, "layout.manual"), (paper.Pdf, "layout.paper")],
)
def test_actual_parser_constructors_select_domain(monkeypatch: pytest.MonkeyPatch, recognizers: dict[str, Mock], parser_type: type[RAGFlowPdfParser], domain: str, engine: str) -> None:
    monkeypatch.setenv("LAYOUT_RECOGNIZER_TYPE", engine)

    parser = parser_type()

    selected = "AscendLayoutRecognizer" if engine == "ascend" else "LayoutRecognizer"
    other = "LayoutRecognizer" if engine == "ascend" else "AscendLayoutRecognizer"
    recognizers[selected].assert_called_once_with(domain)
    recognizers[other].assert_not_called()
    assert parser.layouter is recognizers[selected].return_value
    if parser_type is not RAGFlowPdfParser:
        assert parser.model_species == domain.removeprefix("layout.")


def test_custom_parser_can_select_domain_with_canonical_attribute(monkeypatch: pytest.MonkeyPatch, recognizers: dict[str, Mock]) -> None:
    class CustomPdf(RAGFlowPdfParser):
        model_species = "custom"

    monkeypatch.setenv("LAYOUT_RECOGNIZER_TYPE", "onnx")
    CustomPdf()

    recognizers["LayoutRecognizer"].assert_called_once_with("layout.custom")


def test_manual_parser_retains_layout_and_page_positions(monkeypatch: pytest.MonkeyPatch, recognizers: dict[str, Mock]) -> None:
    monkeypatch.setenv("LAYOUT_RECOGNIZER_TYPE", "onnx")
    parser = manual.Pdf()
    with Image.new("RGB", (300, 300)) as first, Image.new("RGB", (300, 300)) as second:
        parser.page_images = [first, second]
        parser.page_cum_height = [0, 100]
        parser.boxes = [{"text": "  Manual   body  ", "layoutno": "text-1", "page_number": 2, "x0": 10, "x1": 80, "top": 120, "bottom": 150}]
        for name in ("__images__", "_layouts_rec", "_table_transformer_job", "_text_merge", "_concat_downward", "_filter_forpages"):
            monkeypatch.setattr(parser, name, Mock())
        monkeypatch.setattr(parser, "_extract_table_figure", Mock(return_value=[]))

        sections, tables = parser("manual.pdf", zoomin=3, callback=lambda *args, **kwargs: None)

    assert sections == [("Manual body", "text-1", [(2, 10, 80, 20, 50)])]
    assert tables == []
    assert RAGFlowPdfParser.extract_positions("@@2\t10\t80\t20\t50##") == [([1], 10.0, 80.0, 20.0, 50.0)]
