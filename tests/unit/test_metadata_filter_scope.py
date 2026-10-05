"""Metadata conditions must never reopen an empty match or widen a selection."""

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest

from common.metadata_utils import apply_meta_data_filter, convert_conditions, meta_filter


@pytest.fixture
def metas() -> dict[str, Any]:
    return {"version": {"v1": ["old"], "v2": ["new", "draft"]}, "status": {"ready": ["old", "new"], "draft": ["draft"]}}


@pytest.mark.parametrize(
    ("filters", "logic", "expected"),
    [
        ([{"key": "version", "op": "=", "value": "absent"}, {"key": "status", "op": "=", "value": "ready"}], "and", set()),
        ([{"key": "missing", "op": "=", "value": "x"}, {"key": "status", "op": "=", "value": "ready"}], "and", set()),
        ([{"key": "status", "op": "=", "value": "ready"}, {"key": "missing", "op": "=", "value": "x"}], "and", set()),
        ([{"key": "version", "op": "=", "value": "v2"}, {"key": "status", "op": "=", "value": "ready"}], "and", {"new"}),
        ([{"key": "version", "op": "=", "value": "v1"}, {"key": "version", "op": "=", "value": "v2"}, {"key": "status", "op": "=", "value": "ready"}], "and", set()),
        ([{"key": "missing", "op": "=", "value": "x"}, {"key": "status", "op": "=", "value": "ready"}], "or", {"old", "new"}),
        ([{"key": "status", "op": "=", "value": "ready"}, {"key": "missing", "op": "=", "value": "x"}], "or", {"old", "new"}),
        ([{"key": "version", "op": "=", "value": "v2"}, {"key": "status", "op": "=", "value": "ready"}], "or", {"old", "new", "draft"}),
        ([], "and", set()),
        ([], "or", set()),
    ],
)
def test_boolean_conditions(metas: dict[str, Any], filters: list[dict[str, Any]], logic: str, expected: set[str]) -> None:
    assert set(meta_filter(metas, filters, logic)) == expected


@pytest.mark.parametrize("operator", ["is", "=", "not is", "!=", "≠", ">=", "≥", "<=", "≤"])
def test_manual_and_legacy_operator_contracts_are_equivalent(operator: str) -> None:
    metadata = {"number": {"2": ["two"], "5": ["five"]}}
    expected = {"two"} if operator in ("is", "=") else {"five"} if operator in ("not is", "!=", "≠") else {"two", "five"} if operator in (">=", "≥") else {"two"}
    manual = [{"key": "number", "op": operator, "value": "2"}]
    legacy = convert_conditions({"conditions": [{"name": "number", "comparison_operator": operator, "value": "2"}]})
    original = deepcopy((metadata, manual))
    assert set(meta_filter(metadata, manual)) == set(meta_filter(metadata, legacy)) == expected
    assert (metadata, manual) == original


@pytest.mark.parametrize("operator", ["is", "not is", "empty", "not empty", "not contains", "not in"])
def test_missing_field_is_no_match_even_for_negative_or_empty_operators(operator: str) -> None:
    assert meta_filter({"other": {"": ["without-field"]}}, [{"key": "missing", "op": operator, "value": "x"}]) == []
    # Explicit empty values remain distinct from missing fields.
    assert meta_filter({"field": {"": ["empty"], "filled": ["full"]}}, [{"key": "field", "op": "empty", "value": ""}]) == ["empty"]


@pytest.mark.parametrize("method", ["manual", "auto", "semi_auto"])
@pytest.mark.parametrize(
    ("conditions", "logic", "base", "expected", "semi_expected"),
    [
        ([{"key": "version", "op": "is", "value": "v2"}], "and", ["new", "old", "unknown"], {"new"}, None),
        ([{"key": "version", "op": "is", "value": "v2"}], "and", ["old"], {"-999"}, None),
        ([{"key": "version", "op": "is", "value": "absent"}], "and", ["old"], {"-999"}, None),
        ([{"key": "missing", "op": "is", "value": "x"}, {"key": "status", "op": "is", "value": "ready"}], "and", None, {"-999"}, None),
        ([{"key": "missing", "op": "is", "value": "x"}, {"key": "status", "op": "is", "value": "ready"}], "or", ["new", "draft"], {"new"}, {"-999"}),
        ([{"key": "version", "op": "is", "value": "v2"}], "and", None, {"new", "draft"}, None),
        ([{"key": "version", "op": "is", "value": "v2"}], "and", [], {"new", "draft"}, None),
        ([{"key": "version", "op": "is", "value": "absent"}], "and", None, {"-999"}, None),
        ([{"key": "version", "op": "is", "value": "absent"}], "and", [], {"-999"}, None),
    ],
)
async def test_conditions_intersect_base_in_every_mode(
    metas: dict[str, Any], monkeypatch: pytest.MonkeyPatch, method: str, conditions: list[dict[str, Any]], logic: str, base: list[str] | None, expected: set[str], semi_expected: set[str] | None
) -> None:
    config = {"method": method, "manual": conditions, "semi_auto": ["version", "status"], "logic": logic}
    generated = AsyncMock(return_value={"conditions": conditions, "logic": logic})
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", generated)
    original = deepcopy((config, metas, base))
    assert set(await apply_meta_data_filter(config, metas, "question", base_doc_ids=base) or []) == (semi_expected if method == "semi_auto" and semi_expected is not None else expected)
    assert (config, metas, base) == original
    if method == "manual":
        generated.assert_not_called()


@pytest.mark.parametrize("base", [None, [], ["new", "old", "new", "without-metadata"]])
@pytest.mark.parametrize("config", [None, {}, {"method": "manual", "manual": []}, {"method": "unknown"}])
async def test_no_filter_preserves_base(metas: dict[str, Any], monkeypatch: pytest.MonkeyPatch, config: dict[str, Any] | None, base: list[str] | None) -> None:
    generated = AsyncMock()
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", generated)
    assert await apply_meta_data_filter(config, metas, "", base_doc_ids=base) == (base or [])
    generated.assert_not_called()


@pytest.mark.parametrize("base", [None, [], ["old", "without-metadata"]])
async def test_auto_no_generated_conditions_preserve_fallback(metas: dict[str, Any], monkeypatch: pytest.MonkeyPatch, base: list[str] | None) -> None:
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", AsyncMock(return_value={"conditions": []}))
    assert await apply_meta_data_filter({"method": "auto"}, metas, "", base_doc_ids=base) == (base or None)


async def test_semi_auto_does_not_match_unselected_keys(metas: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    generated = AsyncMock(return_value={"conditions": [{"key": "status", "op": "is", "value": "ready"}]})
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", generated)
    assert await apply_meta_data_filter({"method": "semi_auto", "semi_auto": ["version"]}, metas, "") == ["-999"]
    assert generated.call_args.args[1] == {"version": metas["version"]}


async def test_manual_resolver_does_not_change_saved_filter(metas: dict[str, Any]) -> None:
    config = {"method": "manual", "manual": [{"key": "version", "op": "is", "value": "{version}"}]}
    original = deepcopy(config)

    def resolve(condition: dict[str, Any]) -> dict[str, Any]:
        condition["value"] = "v2"
        return condition

    assert await apply_meta_data_filter(config, metas, "", base_doc_ids=["draft", "old", "new", "draft"], manual_value_resolver=resolve) == ["draft", "new"]
    assert config == original
