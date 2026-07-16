"""canvas 模板分类归一化（canvas_type / canvas_types 合并去重）的纯函数测试。"""

from api.db.template_utils import normalize_canvas_template_categories


def test_legacy_canvas_type_only():
    normalized = normalize_canvas_template_categories({"id": 1, "canvas_type": "Recommended"})

    assert normalized["canvas_type"] == "Recommended"
    assert normalized["canvas_types"] == ["Recommended"]


def test_canvas_types_only_drops_dirty_values():
    normalized = normalize_canvas_template_categories({"id": 1, "canvas_types": ["Recommended", "Agent", "Agent", "  ", 1, None]})

    assert normalized["canvas_type"] == "Recommended"
    assert normalized["canvas_types"] == ["Recommended", "Agent"]


def test_merges_legacy_and_new_field_legacy_first():
    normalized = normalize_canvas_template_categories({"id": 1, "canvas_type": "Marketing", "canvas_types": ["Recommended", "Marketing", "Agent"]})

    assert normalized["canvas_type"] == "Marketing"
    assert normalized["canvas_types"] == ["Marketing", "Recommended", "Agent"]


def test_no_valid_categories():
    normalized = normalize_canvas_template_categories({"id": 1, "canvas_type": "   ", "canvas_types": [None, 3, "  "]})

    assert normalized["canvas_type"] is None
    assert normalized["canvas_types"] == []


def test_original_payload_not_mutated():
    payload = {"id": 1, "canvas_type": "Agent"}

    normalize_canvas_template_categories(payload)

    assert payload == {"id": 1, "canvas_type": "Agent"}
