#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
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

"""Regression tests for PaddleOCR bbox normalization."""

from PIL import Image

from deepdoc.parser.paddleocr_parser import PaddleOCRParser


def test_transfer_to_sections_normalizes_inverted_block_bbox():
    parser = PaddleOCRParser(api_url="http://localhost")
    result = {
        "layoutParsingResults": [
            {
                "prunedResult": {
                    "parsing_res_list": [
                        {
                            "block_content": "Example",
                            "block_label": "text",
                            "block_bbox": [40, 80, 10, 20],
                        }
                    ]
                }
            }
        ]
    }

    sections = parser._transfer_to_sections(result, "PaddleOCR-VL", "manual")

    assert sections == [("Example", "text", "@@1\t5.0\t20.0\t10.0\t40.0##")]


def test_crop_skips_invalid_inverted_vertical_range_without_crashing():
    parser = PaddleOCRParser(api_url="http://localhost")
    parser.page_images = [Image.new("RGB", (100, 80), "white")]
    parser.page_from = 0

    pic, positions = parser.crop("@@1\t10\t60\t90\t120##", need_position=True)

    assert pic is not None
    assert positions == []
