"""Membership must retain scalar provenance despite colliding display labels."""

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from api.db.services.doc_metadata_service import DocMetadataService
from common.metadata_utils import MetadataValueIndex, apply_meta_data_filter, meta_filter


@pytest.fixture
def typed_metadata(db: Session, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    rows = [
        ("numeric-zero", {"mixed": 0}),
        ("text-zero", {"mixed": "0"}),
        ("boolean-false", {"mixed": False}),
        ("text-false", {"mixed": "False"}),
        ("numeric-one", {"mixed": 1}),
        ("boolean-true", {"mixed": True}),
    ]
    store = SimpleNamespace(list_by_kb_ids=lambda *_: rows)
    monkeypatch.setattr(DocMetadataService, "_store", staticmethod(lambda: store))
    monkeypatch.setattr(DocMetadataService, "_kb_tenant", classmethod(lambda *_: "tenant"))
    return DocMetadataService.get_flatted_meta_by_kbs(db, ["dataset"])


@pytest.mark.parametrize("operator", ["in", "not in"])
@pytest.mark.parametrize(
    "values,members",
    [
        ([0], {"numeric-zero"}),
        (["0"], {"text-zero"}),
        ([False], {"boolean-false"}),
        (["FALSE"], {"text-false"}),
        ([1], {"numeric-one"}),
        ([True], {"boolean-true"}),
        ([0, "False", True], {"numeric-zero", "text-false", "boolean-true"}),
        ([0.0], {"numeric-zero"}),
        ([1.0], {"numeric-one"}),
    ],
)
async def test_actual_aggregation_preserves_mixed_scalar_membership(typed_metadata: dict[str, Any], operator: str, values: list[Any], members: set[str]) -> None:
    all_documents = {doc for docs in typed_metadata["mixed"].values() for doc in docs}
    expected = members if operator == "in" else all_documents - members
    conditions = [{"key": "mixed", "op": operator, "value": values}]
    snapshot = deepcopy(list(typed_metadata["mixed"].typed_items()))
    assert set(meta_filter(typed_metadata, conditions)) == expected
    assert set(await apply_meta_data_filter({"method": "manual", "manual": conditions}, typed_metadata, "question") or []) == expected
    assert list(typed_metadata["mixed"].typed_items()) == snapshot
    # Existing HTTP aggregation remains JSON serializable; display collisions
    # combine document lists but never replace the private typed membership data.
    assert json.loads(json.dumps(typed_metadata))["mixed"]["0"] == ["numeric-zero", "text-zero"]
    assert json.loads(json.dumps(typed_metadata))["mixed"]["False"] == ["boolean-false", "text-false"]


@pytest.mark.parametrize("method", ["auto", "semi_auto"])
async def test_generated_typed_list_filters_use_original_scalar_types(typed_metadata: dict[str, Any], monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    generated = AsyncMock(return_value={"conditions": [{"key": "mixed", "op": "in", "value": [0, False]}]})
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", generated)
    assert set(await apply_meta_data_filter({"method": method, "semi_auto": ["mixed"]}, typed_metadata, "question") or []) == {"numeric-zero", "boolean-false"}


def test_plain_scalar_indexes_keep_strict_types_and_boolean_number_distinction() -> None:
    for stored, query in [(True, 1), (False, 0), (0, "0"), ("0", 0), (False, "False")]:
        assert meta_filter({"value": {stored: ["doc"]}}, [{"key": "value", "op": "in", "value": [query]}]) == []
    index = MetadataValueIndex()
    index.add("F2", "upper")
    index.add("f2", "lower")
    assert set(meta_filter({"value": index}, [{"key": "value", "op": "in", "value": ["F2"]}])) == {"upper", "lower"}
