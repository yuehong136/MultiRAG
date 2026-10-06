from copy import deepcopy
from typing import Any
from unittest.mock import Mock

import pytest

from common.metadata_utils import MetadataValueIndex, apply_meta_data_filter, convert_conditions, meta_filter


@pytest.mark.parametrize("operator", ["in", "not in"])
@pytest.mark.parametrize("values", [["F2", "F11"], ["f2", "f11"], ["f2", "F11"]])
def test_membership_list_strings_ignore_case(operator: str, values: list[str]) -> None:
    metas = {"product": {"F2": ["doc1"], "f11": ["doc2"], "G1": ["doc3"], "F": ["doc4"]}}
    filters = [{"key": "product", "op": operator, "value": values}]
    original = deepcopy((metas, filters))

    expected = {"doc1", "doc2"} if operator == "in" else {"doc3", "doc4"}
    assert set(meta_filter(metas, filters)) == expected
    assert (metas, filters) == original


@pytest.mark.parametrize("operator", ["in", "not in"])
@pytest.mark.parametrize(
    "stored,values,member",
    [
        (0, [0], True),
        (False, [False], True),
        (5, [5], True),
        (1.5, [1.5], True),
        (True, [True], True),
        (0, [False], False),
        (False, [0], False),
        (1, [True], False),
        (True, [1], False),
        (9007199254740993, [9007199254740993], True),
        ("F2", ["f2", 0, False], True),
        ("F", ["f2"], False),
        ("05", [5], False),
        (" F2 ", ["f2"], False),
        ("", [""], True),
    ],
)
def test_membership_uses_aggregated_scalar_types(operator: str, stored: Any, values: list[Any], member: bool) -> None:
    entries = MetadataValueIndex()
    entries.add(stored, "selected")
    entries.add("outside", "other")
    metas = {"field": entries}
    filters = [{"key": "field", "op": operator, "value": values}]
    original = deepcopy((metas, filters))
    expected = ({"selected"} if member else set()) if operator == "in" else ({"other"} if member else {"selected", "other"})
    assert set(meta_filter(metas, filters)) == expected
    assert (metas, filters) == original


@pytest.mark.parametrize("operator", ["in", "not in"])
@pytest.mark.parametrize(
    ("metadata_value", "filter_value", "member"),
    [
        (5, [5, 10], True),
        (5, ["5"], False),
        ("5", [5], False),
        ("F2", ["f2", 5], True),
        (5, ["F2", 5], True),
        ("5", ["F2", 5], False),
        (None, ["F2", 5], False),
        (True, ["F2", True], True),
        (1.5, ["F2", 1.5], True),
        ("F2", [], False),
    ],
)
def test_membership_preserves_non_string_types(operator: str, metadata_value: Any, filter_value: list[Any], member: bool) -> None:
    metas = {"value": {metadata_value: ["doc"]}}
    filters = [{"key": "value", "op": operator, "value": filter_value}]
    original = deepcopy((metas, filters))

    assert meta_filter(metas, filters) == (["doc"] if member == (operator == "in") else [])
    assert (metas, filters) == original


@pytest.mark.parametrize(
    ("operator", "metadata_value", "filter_value", "matched"),
    [
        ("in", ["F2", "f11"], ["f2", "F11"], True),
        ("in", ["F2", "G1"], ["f2", "F11"], False),
        ("not in", ["F2", "G1"], ["f2", "F11"], False),
        ("not in", ["G1", "g2"], ["f2", "F11"], True),
        ("in", ["F2", 5], ["f2", 5], True),
        ("in", ["F2", "5"], ["f2", 5], False),
        ("not in", ["G1", 6], ["f2", 5], True),
        ("not in", ["G1", 5], ["f2", 5], False),
        ("in", ["F2", "f11"], "F2,F11", True),
        ("not in", ["G1", "g2"], "F2,F11", True),
        ("in", [], [], True),
        ("not in", [], [], True),
    ],
)
def test_membership_list_input_keeps_all_items_semantics(operator: str, metadata_value: list[Any], filter_value: Any, matched: bool) -> None:
    # Production aggregation flattens lists into string keys. Supply items directly
    # to exercise the matcher's existing list-input branch without unhashable keys.
    entries = Mock(spec=dict)
    entries.items.return_value = [(metadata_value, ["doc"])]
    filters = [{"key": "value", "op": operator, "value": filter_value}]
    original = deepcopy((metadata_value, filter_value))

    assert meta_filter({"value": entries}, filters) == (["doc"] if matched else [])
    assert (metadata_value, filter_value) == original


@pytest.mark.parametrize(
    ("operator", "metadata_value", "filter_value", "matched"),
    [
        ("in", "F2", "F2,F11", True),
        ("not in", "G1", "F2,F11", True),
        ("contains", "F2", "f", True),
        ("not contains", "F2", "g", True),
        ("contains", ["F2"], "f", False),
        ("not contains", ["F2"], "f", True),
        ("start with", ["F2", "F11"], "f2", True),
        ("end with", ["F2", "F11"], "f11", True),
        ("empty", [], "", True),
        ("not empty", ["F2"], "", True),
        ("=", "F2", "f2", True),
        ("≠", "F2", "f2", False),
        (">", "10", "5", True),
        ("<", "2", "5", True),
        ("≥", "5", "5", True),
        ("≤", "5", "5", True),
        (">", "2026-10-03", "2026-10-02", True),
        (">", "F2", "2026-10-02", False),
    ],
)
def test_other_operand_and_operator_behavior_is_preserved(operator: str, metadata_value: Any, filter_value: Any, matched: bool) -> None:
    entries = Mock(spec=dict)
    entries.items.return_value = [(metadata_value, ["doc"])]
    filters = [{"key": "value", "op": operator, "value": filter_value}]

    assert meta_filter({"value": entries}, filters) == (["doc"] if matched else [])


@pytest.mark.parametrize("operator", ["in", "not in"])
async def test_manual_membership_filter_uses_list_normalization(operator: str) -> None:
    metas = {"product": {"F2": ["doc1"], "f11": ["doc2"], "G1": ["doc3"]}}
    filters = convert_conditions({"conditions": [{"name": "product", "comparison_operator": operator, "value": ["F2", "F11"]}]})
    config = {"method": "manual", "manual": filters}

    expected = {"doc1", "doc2"} if operator == "in" else {"doc3"}
    assert set(await apply_meta_data_filter(config, metas, "") or []) == expected
    assert await apply_meta_data_filter(config, {"product": {}}, "") == ["-999"]
