"""Memory aggregation accepts mixed-quality scalar and list field values."""

from memory.utils.aggregation_utils import aggregate_by_field


def test_list_values_trim_whitespace_and_ignore_invalid_entries() -> None:
    messages = [
        {"tags_kwd": [" alpha ", "beta", "", "  "]},
        {"tags_kwd": ["alpha", " beta "]},
        {"tags_kwd": ["gamma", None, 1, False, {}]},
    ]
    assert aggregate_by_field(messages, "tags_kwd") == [("alpha", 2), ("beta", 2), ("gamma", 1)]


def test_scalar_values_ignore_blank_non_string_and_missing_fields() -> None:
    messages = [{"type": value} for value in ["  ", "", None, 1, False, {}, " assistant "]] + [{}]
    assert aggregate_by_field(messages, "type") == [("assistant", 1)]


def test_empty_and_preaggregated_results_keep_the_existing_contract() -> None:
    assert aggregate_by_field(None, "type") == []
    assert aggregate_by_field([], "type") == []
    assert aggregate_by_field([{"value": "assistant", "count": 3}, {"type": ["user", "user"]}], "type") == [("assistant", 3), ("user", 2)]
