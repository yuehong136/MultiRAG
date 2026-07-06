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

"""Regression tests for PDF parser resilience on low-evidence pages."""

import importlib.util
import os
import sys
from unittest import mock

import numpy as np

_MOCK_MODULES = [
    "pdfplumber",
    "xgboost",
    "huggingface_hub",
    "PIL",
    "PIL.Image",
    "pypdf",
    "common",
    "common.file_utils",
    "common.misc_utils",
    "common.settings",
    "common.token_utils",
    "deepdoc",
    "deepdoc.vision",
    "deepdoc.parser",
    "deepdoc.parser.utils",
    "core",
    "core.nlp",
    "core.nlp.rag_tokenizer",
    "core.prompts",
    "core.prompts.generator",
]
for _m in _MOCK_MODULES:
    if _m not in sys.modules:
        sys.modules[_m] = mock.MagicMock()


def _find_project_root(marker="pyproject.toml"):
    cur = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.exists(os.path.join(cur, marker)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise FileNotFoundError(f"Could not locate project root (missing {marker})")
        cur = parent


_MODULE_PATH = os.path.join(_find_project_root(), "deepdoc", "parser", "pdf_parser.py")
_spec = importlib.util.spec_from_file_location("pdf_parser_layout_resilience", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_Parser = _mod.RAGFlowPdfParser


class _FakeRecognizer:
    @staticmethod
    def sort_Y_firstly(boxes, _threshold):
        return list(boxes)

    @staticmethod
    def find_overlapped(_char, _boxes):
        return None


class _FakeOCRWithQuad:
    def __init__(self):
        self.cropped_quad = None

    def detect(self, _img, _device_id=None):
        return [
            (
                np.array(
                    [[20, 10], [10, 10], [10, 40], [20, 40]],
                    dtype=np.float32,
                ),
                ("", 0),
            )
        ]

    def get_rotate_crop_image(self, _img, quad):
        self.cropped_quad = quad
        return np.zeros((8, 8, 3), dtype=np.uint8)

    def recognize_batch(self, img_list, _device_id=None):
        return ["示例文本" for _ in img_list]


class _FakeEmptyOCR:
    def detect(self, _img, _device_id=None):
        return []

    def recognize_batch(self, _img_list, _device_id=None):
        return []


def _make_parser():
    parser = _Parser.__new__(_Parser)
    parser.lefted_chars = []
    parser.boxes = []
    parser.mean_height = []
    parser.mean_width = []
    return parser


def test_normalize_ocr_quad_accepts_shuffled_points():
    norm = _Parser._normalize_ocr_quad(
        np.array([[20, 10], [10, 10], [10, 40], [20, 40]], dtype=np.float32),
        zoomin=1,
    )

    assert norm is not None
    assert norm["x0"] == 10.0
    assert norm["x1"] == 20.0
    assert norm["top"] == 10.0
    assert norm["bottom"] == 40.0


def test_ocr_preserves_rotated_quad_and_recovers_text(monkeypatch):
    monkeypatch.setattr(_mod, "Recognizer", _FakeRecognizer)

    parser = _make_parser()
    parser.ocr = _FakeOCRWithQuad()
    parser.mean_height = [0]
    parser.mean_width = [0]

    parser._RAGFlowPdfParser__ocr(1, np.zeros((64, 64, 3), dtype=np.uint8), [], ZM=1)

    assert len(parser.boxes) == 1
    assert len(parser.boxes[0]) == 1
    assert parser.boxes[0][0]["text"] == "示例文本"
    assert parser.boxes[0][0]["x0"] == 10.0
    assert parser.boxes[0][0]["x1"] == 20.0
    assert parser.mean_height[0] == 30.0
    assert parser.mean_width[0] == 10.0
    assert np.array_equal(
        parser.ocr.cropped_quad,
        np.array([[20, 10], [10, 10], [10, 40], [20, 40]], dtype=np.float32),
    )


def test_ocr_uses_neighbor_metrics_when_page_has_no_text_boxes():
    parser = _make_parser()
    parser.ocr = _FakeEmptyOCR()
    parser.mean_height = [0, 14]
    parser.mean_width = [0, 9]

    parser._RAGFlowPdfParser__ocr(1, np.zeros((32, 32, 3), dtype=np.uint8), [], ZM=1)

    assert parser.boxes == [[]]
    assert parser.mean_height[0] == 14.0
    assert parser.mean_width[0] == 9.0


def test_assign_column_uses_textual_boxes_only():
    parser = _make_parser()
    boxes = [
        {"page_number": 1, "x0": 10.0, "x1": 120.0, "text": "左栏一", "layout_type": "text"},
        {"page_number": 1, "x0": 12.0, "x1": 122.0, "text": "左栏二", "layout_type": "text"},
        {"page_number": 1, "x0": 15.0, "x1": 125.0, "text": "左栏三", "layout_type": "text"},
        {"page_number": 1, "x0": 300.0, "x1": 410.0, "text": "右栏一", "layout_type": "text"},
        {"page_number": 1, "x0": 302.0, "x1": 412.0, "text": "右栏二", "layout_type": "text"},
        {"page_number": 1, "x0": 305.0, "x1": 415.0, "text": "右栏三", "layout_type": "text"},
        {"page_number": 2, "x0": 20.0, "x1": 520.0, "text": "表格页", "layout_type": "table"},
        {"page_number": 2, "x0": 30.0, "x1": 530.0, "text": "", "layout_type": "figure"},
    ]

    parser._assign_column(boxes)

    page1_cols = {box["col_id"] for box in boxes if box["page_number"] == 1}
    page2_cols = {box["col_id"] for box in boxes if box["page_number"] == 2}

    assert page1_cols == {0, 1}
    assert page2_cols == {0}
