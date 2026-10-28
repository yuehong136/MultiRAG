"""Numbered captions must be retained without classifying ordinary words as captions."""

import pytest

from deepdoc.vision.table_structure_recognizer import TableStructureRecognizer


@pytest.mark.parametrize(
    "text",
    [
        "Fig. 1: Overview",
        "Fig.2 Results",
        "Fig 3 — Details",
        "fig4. Accuracy",
        "  FIG. 12.1: Architecture  ",
        "Figure 5. Model",
        "FIGURE\t6: Analysis",
        "table 7: Summary",
        "Table\n8.1: Measurements",
        "图 1：示例",
        "表 2: 数据",
        "图表 12：汇总",
    ],
)
def test_numbered_caption(text: str) -> None:
    assert TableStructureRecognizer.is_caption({"text": text})


@pytest.mark.parametrize(
    "text",
    [
        "Figure out the result",
        "Fig. without a number",
        "Figment 1",
        "Figurehead 2",
        "Tables 3",
        "Tableau 4",
        "Table 2nd result",
        "Figure 123data",
        "Fig. 1_value",
        "Refer to Figure 1",
        "",
    ],
)
def test_ordinary_text_is_not_caption(text: str) -> None:
    assert not TableStructureRecognizer.is_caption({"text": text})


@pytest.mark.parametrize("layout_type", ["caption", "figure caption", "table caption"])
def test_layout_caption_does_not_require_a_number(layout_type: str) -> None:
    assert TableStructureRecognizer.is_caption({"text": "Unnumbered result", "layout_type": layout_type})


@pytest.mark.parametrize("caption", ["Fig. 1: Revenue", "Figure 2: Revenue", "Table 3: Revenue", "表 4：收入"])
def test_table_output_keeps_numbered_caption_separate_from_cells(caption: str) -> None:
    boxes = [{"text": caption}, {"text": "12", "x0": 10, "x1": 30, "top": 40, "bottom": 50, "page_number": 1}]
    result = TableStructureRecognizer.construct_table(boxes, is_english=True, html=True)
    assert f"<caption> {caption}</caption>" in result
    assert result.count(caption) == 1
    assert ">12</td>" in result
