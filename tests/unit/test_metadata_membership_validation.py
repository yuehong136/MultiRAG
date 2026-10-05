"""Invalid exclusion operands must never turn metadata predicates into true."""

from typing import Any

import pytest

from common.metadata_utils import apply_meta_data_filter, meta_filter


@pytest.mark.parametrize("operand", [[None], [{}], [["ready"]], [float("inf")], [float("-inf")], [float("nan")], ["ready", None], {"ready": True}])
@pytest.mark.parametrize("operator", ["in", "not in"])
@pytest.mark.parametrize("logic", ["and", "or"])
async def test_invalid_membership_invalidates_complete_filter(operand: Any, operator: str, logic: str) -> None:
    metas = {"category": {"ready": ["selected"], "other": ["outside"]}}
    conditions = [{"key": "category", "op": "is", "value": "ready"}, {"key": "category", "op": operator, "value": operand}]
    assert meta_filter(metas, conditions, logic) == []
    assert await apply_meta_data_filter({"method": "manual", "manual": conditions, "logic": logic}, metas, "question", base_doc_ids=["selected", "outside"]) == ["-999"]
