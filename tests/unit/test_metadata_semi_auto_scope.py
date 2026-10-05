"""Semi-auto selections cannot become unfiltered retrieval when stale or empty."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from common.metadata_utils import apply_meta_data_filter


@pytest.mark.parametrize("selection", [[], ["missing"], ["category", "missing"], [{"key": "missing", "op": "is"}], [{}], [None], "category"])
@pytest.mark.parametrize("base", [None, ["selected"]])
async def test_invalid_semi_auto_selection_is_zero_without_generation(monkeypatch: pytest.MonkeyPatch, selection: Any, base: list[str] | None) -> None:
    generate = AsyncMock(return_value={"conditions": []})
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", generate)
    result = await apply_meta_data_filter({"method": "semi_auto", "semi_auto": selection}, {"category": {"ready": ["selected", "other"]}}, "question", base_doc_ids=base)
    assert result == ["-999"]
    generate.assert_not_called()


@pytest.mark.parametrize("values", [{}, {"ready": []}, None, []])
async def test_field_without_candidate_documents_is_zero(monkeypatch: pytest.MonkeyPatch, values: Any) -> None:
    generate = AsyncMock()
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", generate)
    assert await apply_meta_data_filter({"method": "semi_auto", "semi_auto": ["category"]}, {"category": values}, "question") == ["-999"]
    generate.assert_not_called()


@pytest.mark.parametrize(
    "conditions", [[], [{"key": "unselected", "op": "is", "value": "yes"}], [{"key": "category", "op": "is", "value": "ready"}, {"key": "unselected", "op": "is", "value": "yes"}]]
)
async def test_empty_or_unselected_generated_predicates_are_zero(monkeypatch: pytest.MonkeyPatch, conditions: list[dict[str, Any]]) -> None:
    generate = AsyncMock(return_value={"conditions": conditions, "logic": "or"})
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", generate)
    assert await apply_meta_data_filter({"method": "semi_auto", "semi_auto": ["category"]}, {"category": {"ready": ["selected", "other"]}}, "question", base_doc_ids=["selected"]) == ["-999"]


@pytest.mark.parametrize("base", [None, [], ["selected"]])
async def test_empty_inference_never_restores_base(monkeypatch: pytest.MonkeyPatch, base: list[str] | None) -> None:
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", AsyncMock(return_value={"conditions": []}))
    assert await apply_meta_data_filter({"method": "semi_auto", "semi_auto": ["category"]}, {"category": {"ready": ["selected"]}}, "question", base_doc_ids=base) == ["-999"]


@pytest.mark.parametrize(("fresh", "expected"), [({}, ["-999"]), ({"category": {}}, ["-999"]), ({"category": {"ready": ["other"]}}, ["-999"]), ({"category": {"ready": ["selected"]}}, ["selected"])])
async def test_revalidate_fields_and_values_after_model_io(monkeypatch: pytest.MonkeyPatch, fresh: dict[str, Any], expected: list[str]) -> None:
    generate = AsyncMock(return_value={"conditions": [{"key": "category", "op": "is", "value": "ready"}]})
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", generate)
    refresh = AsyncMock(return_value=fresh)
    result = await apply_meta_data_filter({"method": "semi_auto", "semi_auto": ["category"]}, {"category": {"ready": ["selected"]}}, "question", base_doc_ids=["selected"], metadata_refresher=refresh)
    assert result == expected
    refresh.assert_awaited_once()
    generate.assert_awaited_once()
