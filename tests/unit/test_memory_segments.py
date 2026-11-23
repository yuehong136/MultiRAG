"""Exact span/provenance and opt-in compatibility, independent of model quality."""

from copy import deepcopy
from itertools import pairwise
from typing import Any

import pytest

from memory.maintenance.domain import assemble_candidates, canonical_request, validate_manifest
from memory.maintenance.incremental import selection_request
from memory.maintenance.segments import fragment_metadata, segmentation_request, split_source, validate_fragment
from memory.revisions.domain import RevisionValidationError, content_hash


def source(text: str, *, generation: int = 1) -> dict[str, Any]:
    return {"source_id": 2**53 + 9, "source_revision": 1, "source_generation": generation, "content_sha256": content_hash(text), "scope": {"user_id": "u", "agent_id": "a", "session_id": "s"}}


def test_default_whole_request_remains_identical() -> None:
    options: dict[str, Any] = {"scope": {"user_id": None, "agent_id": None, "session_id": None}}
    assert selection_request(**options) == selection_request(**options, segmentation={"mode": "whole"})
    assert "segmentation" not in selection_request(**options)


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "whole", "fragment_token_limit": 2048},
        {"mode": "segmented", "max_parts_per_source": True},
        {"mode": "segmented", "fragment_token_limit": 255},
        {"mode": "segmented", "splitter_version": "forged"},
    ],
)
def test_segmentation_rejects_client_forged_or_ambiguous_settings(value: dict[str, Any]) -> None:
    with pytest.raises(RevisionValidationError):
        segmentation_request(value)


def test_unicode_core_tiles_exact_original_with_bounded_overlap_and_late_needle() -> None:
    text = "\n\n".join(f"第{i}段😀：只有审核后才采用；请勿自动发布。" + (f"编号{i}内容" * 18) for i in range(12)) + "\n最后证据：保留失败费用。"
    configuration = segmentation_request({"mode": "segmented", "fragment_token_limit": 512})
    assert configuration is not None
    parts = split_source("bank", source(text), text, configuration, count_tokens=len, fits=lambda part: True)
    assert len(parts) > 3 and parts == split_source("bank", source(text), text, configuration, count_tokens=len, fits=lambda part: True)
    assert "".join(text[part["core_start"] : part["core_end"]] for part in parts) == text
    assert parts[0]["core_start"] == 0 and parts[-1]["core_end"] == len(text)
    assert len({part["fragment_id"] for part in parts}) == len(parts)
    assert all(left["core_end"] == right["core_start"] for left, right in pairwise(parts))
    assert all(len(text[part["read_start"] : part["read_end"]]) <= 512 for part in parts)
    assert parts[-1]["part_index"] == len(parts) - 1
    for part in parts:
        validate_fragment(part, content=text[part["read_start"] : part["read_end"]])


def test_boundary_overlap_preserves_negation_without_crediting_it_twice() -> None:
    text = "甲" * 210 + "如果客户尚未授权，\n不要执行删除。" + "乙" * 500
    configuration = segmentation_request({"mode": "segmented", "fragment_token_limit": 320})
    assert configuration is not None
    parts = split_source("bank", source(text), text, configuration, count_tokens=len, fits=lambda part: True)
    assert any("如果客户尚未授权，\n不要执行删除。" in text[item["read_start"] : item["read_end"]] for item in parts)
    assert sum(part["core_end"] - part["core_start"] for part in parts) == len(text)
    assert sum(part["read_end"] - part["read_start"] for part in parts) > len(text)


def test_limit_or_unfitting_context_never_returns_a_truncated_prefix() -> None:
    text = "证据" * 2000
    configuration = segmentation_request({"mode": "segmented", "fragment_token_limit": 256, "max_parts_per_source": 2})
    assert configuration is not None
    with pytest.raises(RevisionValidationError, match="segment_parts_limit"):
        split_source("bank", source(text), text, configuration, count_tokens=len, fits=lambda part: True)
    with pytest.raises(RevisionValidationError, match="segment_no_fragment_fits"):
        split_source("bank", source(text), text, configuration, count_tokens=len, fits=lambda part: False)


def test_candidate_keeps_full_source_hash_and_exact_fragment_provenance() -> None:
    text = "原始完整来源：" + "甲" * 300 + "当前片中记录的事实。" + "乙" * 300
    observed = source(text)
    part = fragment_metadata("bank", observed, text, start=300, end=360, index=1, count=3)
    body = text[part["read_start"] : part["read_end"]]
    evidence = {
        "input_id": "part-" + part["fragment_id"][:24],
        **observed,
        "fragment": part,
        "valid_at": None,
        "coverage": "included",
        "defer_reason": None,
        "snapshot": {"content": body, "message_type": "raw", "origin": "legacy_combined", "role_status": "role_unknown"},
    }
    manifest: dict[str, Any] = {"inputs": [evidence], "entries": [], "estimated_input_tokens": 512}
    checked = validate_manifest(manifest, canonical_request([observed["source_id"]], [], "general", 8192, 1024), lambda value: len(value) // 4)
    candidate = assemble_candidates(
        [{"action": "add", "message_type": "semantic", "content": "仅供人工审核的事实候选", "source_input_ids": [evidence["input_id"]], "reason": "来源片段提供证据"}], checked, len, 1024
    )[0]
    assert candidate["sources"][0]["content_sha256"] == content_hash(text) != content_hash(body)
    assert candidate["sources"][0]["fragment"] == part
    assert candidate["assessment"] == {"kind": "hypothesis", "verification_status": "unverified"}
    altered = deepcopy(manifest)
    altered["inputs"][0]["snapshot"]["content"] += "伪造"
    with pytest.raises(RevisionValidationError, match="maintenance_fragment_hash_mismatch"):
        validate_manifest(altered, canonical_request([observed["source_id"]], [], "general", 8192, 1024), lambda value: len(value) // 4)
    assert part["fragment_id"] != fragment_metadata("bank", source(text, generation=2), text, start=300, end=360, index=1, count=3)["fragment_id"]
