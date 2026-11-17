"""Compression preserves explicit order, complete retained records and provenance."""

from copy import deepcopy
from typing import Any

import pytest

from memory.compaction.domain import model_body, package_for, selection, summary_text
from memory.revisions.domain import RevisionValidationError


def roots() -> list[dict[str, Any]]:
    return [
        {
            "binding": {"source_id": identity, "scope": {"user_id": "u", "agent_id": "a", "session_id": identity}, "role_status": "role_unknown", "format": "legacy_combined"},
            "content": "中文原文🙂 User Input: data " + identity,
            "declared_roles": [],
        }
        for identity in ("20", "3")
    ]


def test_order_is_not_numeric_sort_and_whole_suffix_keeps_original_text() -> None:
    selected = selection("stable", ["20", "3"], 1, 16384, 1024, 4096)
    assert selected["source_ids"] == ["20", "3"]
    inputs = roots()
    unchanged = deepcopy(inputs)
    package = package_for(inputs, prefix_count=1, summary="未核验的摘要")
    assert package["retained"][0]["content"] == inputs[1]["content"]
    assert [item["source_id"] for item in package["lineage"]] == ["20", "3"]
    assert package["covered"] == [{"source_id": "20", "offset": 0, "end_offset": len(inputs[0]["content"]), "offset_unit": "unicode_codepoint"}]
    assert package["omitted"] == {"source_count": 0, "codepoints": 0}
    assert inputs == unchanged


@pytest.mark.parametrize("ids,prefix,input_budget", [(["1", "1"], 1, 1024), ([1], 1, 1024), (["01"], 1, 1024), ([str(2**63)], 1, 1024), (["1"], 0, 1024), (["1"], True, 1024), (["1"], 1, True)])
def test_invalid_ordered_selection_is_rejected(ids: Any, prefix: Any, input_budget: Any) -> None:
    with pytest.raises(RevisionValidationError):
        selection("stable", ids, prefix, input_budget, 1024, 4096)


def test_unknown_tool_boundary_is_rejected_only_when_selected_for_summary() -> None:
    inputs = roots()
    inputs[1]["declared_roles"] = ["tool"]
    assert "20" in model_body(inputs, 1)
    with pytest.raises(RevisionValidationError, match="tool_boundary_unknown"):
        model_body(inputs, 2)


def test_cross_subject_records_are_not_combined_into_one_summary() -> None:
    inputs = roots()
    inputs[1]["binding"]["scope"]["user_id"] = "other"
    with pytest.raises(RevisionValidationError, match="scope_mismatch"):
        model_body(inputs, 1)


@pytest.mark.parametrize("text", ["", "  ", "<think>reasoning only</think>", "<think>unfinished", "**ERROR**: provider failure", None])
def test_empty_or_reasoning_only_output_is_not_an_artifact(text: Any) -> None:
    with pytest.raises(RevisionValidationError):
        summary_text(text)


def test_visible_output_after_reasoning_is_the_only_summary() -> None:
    assert summary_text("<think>private reasoning</think>真实摘要") == "真实摘要"
