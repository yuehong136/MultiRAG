"""Document patch acceptance and preservation at the pure service boundary."""

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from api.utils.document_parser_config import DocumentParserConfigPatch, merge_document_parser_config
from common.float_utils import normalize_overlapped_percent
from common.metadata_utils import build_metadata_config, turn2jsonschema


def test_partial_patch_retains_production_configuration_without_aliasing() -> None:
    stored = {
        "raptor": {"use_raptor": True, "max_token": 256, "threshold": 0.13, "scope": "dataset", "future": [1]},
        "graphrag": {"use_graphrag": True, "entity_types": ["person"], "method": "general", "community": True, "resolution": True},
        "metadata": {"type": "object", "properties": {"year": {"type": "integer"}}, "required": ["year"]},
        "built_in_metadata": [{"key": "filename"}],
        "enable_metadata": True,
        "llm_id": "local-model",
        "field_map": {"id": "ID"},
        "flow": {"legacy": [1]},
        "other": None,
    }
    patch = {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}, "metadata": {"properties": {"title": {"type": "string"}}}}
    original, incoming = deepcopy(stored), deepcopy(patch)
    result = merge_document_parser_config(stored, patch)
    expected = deepcopy(stored)
    expected["raptor"]["use_raptor"] = False
    expected["graphrag"]["use_graphrag"] = False
    expected["metadata"]["properties"]["title"] = {"type": "string"}
    assert result == expected
    assert stored == original and patch == incoming
    result["flow"]["legacy"].append(2)
    result["raptor"]["future"].append(2)
    result["metadata"]["required"].append("title")
    assert stored == original and patch == incoming


def test_omission_and_empty_nested_patches_are_noops() -> None:
    stored = {"chunk_token_num": 8192, "raptor": {"use_raptor": True, "threshold": 0.2}, "parent_child": {"use_parent_child": True, "children_delimiter": ";"}}
    for patch in ({}, {"raptor": {}}, {"parent_child": {}}, DocumentParserConfigPatch()):
        result = merge_document_parser_config(stored, patch)
        assert result == stored and result is not stored
    assert merge_document_parser_config({}, {"raptor": {}, "graphrag": {}, "parent_child": {}, "metadata": {}}) == {}
    assert DocumentParserConfigPatch(raptor={"use_raptor": False}).model_dump(exclude_unset=True) == {"raptor": {"use_raptor": False}}


@pytest.mark.parametrize(
    "patch",
    [
        {"ext": {}},
        {"operator:123": {}},
        {"new_feature": 1},
        {"image_table_context_window": 32},
        {"formula_enable": True},
        {"field_map": {}},
        {"raptor": {"new_feature": 1}},
        {"graphrag": {"scope": "file"}},
        {"toc_extraction": 1},
        {"html4excel": "false"},
        {"enable_children": 0},
        {"mineru_table_enable": "true"},
        {"chunk_token_num": True},
        {"chunk_token_num": "512"},
        {"chunk_token_num": 512.0},
        {"filename_embd_weight": True},
        {"overlapped_percent": "0.1"},
        {"overlapped_percent": True},
        {"raptor": {"threshold": float("nan")}},
        {"filename_embd_weight": float("inf")},
        {"overlapped_percent": float("-inf")},
        {"overlapped_percent": float("nan")},
        {"delimiter": 3},
        {"llm_id": 1},
        {"tag_kb_ids": [1]},
        {"raptor": {"prompt": 1}},
        {"raptor": {"prompt": "  "}},
        {"metadata": [{"key": "name", "description": 1}]},
        {"built_in_metadata": [{"key": "name", "enum": [1]}]},
        {"metadata": {"properties": {"x": {"bad": float("nan")}}}},
        {"metadata": {"type": "array", "properties": {}}},
        {"mineru_parse_method": "paper"},
        {"mineru_lang": "en"},
    ],
)
def test_unsafe_or_unsupported_incoming_fields_fail_before_merge(patch: dict[str, Any]) -> None:
    stored = {"unchanged": [1]}
    with pytest.raises(ValidationError):
        merge_document_parser_config(stored, patch)
    assert stored == {"unchanged": [1]}


@pytest.mark.parametrize("field", list(DocumentParserConfigPatch.model_fields))
def test_explicit_null_is_rejected_for_every_document_field(field: str) -> None:
    with pytest.raises(ValidationError):
        DocumentParserConfigPatch.model_validate({field: None})


@pytest.mark.parametrize("patch", [{"raptor": {"use_raptor": None}}, {"graphrag": {"entity_types": None}}, {"parent_child": {"children_delimiter": None}}])
def test_null_nested_fields_are_not_omissions(patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DocumentParserConfigPatch.model_validate(patch)


@pytest.mark.parametrize(("value", "percent"), [(0, 0), (0.1, 10), (0.3, 30), (1, 1), (10, 10), (90, 90)])
def test_overlap_keeps_actual_consumer_units(value: float, percent: int) -> None:
    merged = merge_document_parser_config({}, {"overlapped_percent": value})
    assert merged["overlapped_percent"] == value
    assert normalize_overlapped_percent(merged["overlapped_percent"]) == percent


@pytest.mark.parametrize(
    "patch",
    [
        {"chunk_token_num": 0},
        {"chunk_token_num": 8193},
        {"auto_keywords": 33},
        {"auto_questions": 11},
        {"topn_tags": 0},
        {"topn_tags": 11},
        {"filename_embd_weight": 1.01},
        {"image_context_size": -1},
        {"table_context_size": -1},
        {"task_page_size": 0},
        {"overlapped_percent": -0.1},
        {"overlapped_percent": 91},
        {"raptor": {"max_token": 0}},
        {"raptor": {"max_token": 2049}},
        {"raptor": {"threshold": -0.1}},
        {"raptor": {"max_cluster": 1025}},
        {"raptor": {"random_seed": -1}},
        {"raptor": {"scope": "document"}},
        {"pages": [[1, 1]]},
        {"pages": [[0, 2]]},
        {"pages": [[3, 2]]},
        {"pages": [[1]]},
        {"pages": [[True, 2]]},
    ],
)
def test_production_parameter_boundaries(patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DocumentParserConfigPatch.model_validate(patch)


def test_full_document_production_payload_is_partial_and_keeps_local_raptor() -> None:
    patch = {
        "auto_keywords": 32,
        "auto_questions": 10,
        "chunk_token_num": 8192,
        "delimiter": "\n",
        "html4excel": True,
        "layout_recognize": "configured-model@MinerU",
        "tag_kb_ids": ["kb"],
        "topn_tags": 10,
        "filename_embd_weight": 1,
        "task_page_size": 1,
        "pages": [[1, 2], [3, 9]],
        "image_context_size": 0,
        "table_context_size": 128,
        "toc_extraction": True,
        "mineru_parse_method": "ocr",
        "mineru_formula_enable": False,
        "mineru_table_enable": False,
        "mineru_lang": "Japanese",
        "enable_metadata": True,
        "metadata": [{"key": "author", "descriptions": "Author", "enum": ["Alice"]}],
        "built_in_metadata": [{"key": "filename"}],
        "llm_id": "model",
        "raptor": {
            "use_raptor": True,
            "max_token": 256,
            "threshold": 0.1,
            "max_cluster": 64,
            "random_seed": 0,
            "scope": "dataset",
            "auto_disable_for_structured_data": False,
            "prompt": "{cluster_content}",
        },
        "graphrag": {"use_graphrag": True, "entity_types": ["person"], "method": "general", "community": True, "resolution": True},
    }
    assert merge_document_parser_config({}, patch) == patch


@pytest.mark.parametrize("method", ["auto", "txt", "ocr"])
def test_mineru_actual_methods(method: str) -> None:
    assert merge_document_parser_config({}, {"mineru_parse_method": method}) == {"mineru_parse_method": method}


@pytest.mark.parametrize("patch", [{"parent_child": {"use_parent_child": True}}, {"enable_children": True}])
def test_child_enable_normalizes_both_views_and_retains_delimiter(patch: dict[str, Any]) -> None:
    stored = {"parent_child": {"use_parent_child": True, "children_delimiter": ";", "historic": 1}, "enable_children": True, "children_delimiter": ";"}
    assert merge_document_parser_config(stored, patch) == stored


@pytest.mark.parametrize("patch", [{"parent_child": {"children_delimiter": "!"}}, {"children_delimiter": "!"}])
def test_child_partial_delimiter_does_not_disable_saved_enabled_mode(patch: dict[str, Any]) -> None:
    stored = {"parent_child": {"use_parent_child": True, "children_delimiter": ";"}, "enable_children": True, "children_delimiter": ";"}
    assert merge_document_parser_config(stored, patch) == {"parent_child": {"use_parent_child": True, "children_delimiter": "!"}, "enable_children": True, "children_delimiter": "!"}


@pytest.mark.parametrize("patch", [{"parent_child": {"use_parent_child": False}}, {"enable_children": False}])
def test_child_disable_matches_existing_flatten_convention(patch: dict[str, Any]) -> None:
    assert merge_document_parser_config({"parent_child": {"use_parent_child": True, "children_delimiter": ";"}, "raptor": {"use_raptor": True}}, patch) == {
        "parent_child": {},
        "enable_children": False,
        "children_delimiter": "",
        "raptor": {"use_raptor": True},
    }


@pytest.mark.parametrize(
    "patch",
    [
        {"parent_child": {"use_parent_child": True}, "enable_children": False},
        {"parent_child": {"children_delimiter": ";"}, "children_delimiter": "!"},
        {"enable_children": True, "children_delimiter": ""},
    ],
)
def test_conflicting_or_empty_enabled_children_fail(patch: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        merge_document_parser_config({}, patch)


def test_unvalidated_model_cannot_bypass_patch_validation() -> None:
    patch = DocumentParserConfigPatch.model_construct(chunk_token_num=-1)
    with pytest.raises(ValidationError):
        merge_document_parser_config({}, patch)


def test_new_metadata_schema_is_usable_by_real_metadata_consumers() -> None:
    result = merge_document_parser_config({}, {"metadata": {"properties": {"author": {"type": "string"}}}})
    assert result == {"metadata": {"type": "object", "properties": {"author": {"type": "string"}}}}
    assert turn2jsonschema(build_metadata_config(result)) == result["metadata"]


def test_partial_metadata_constraints_preserve_stored_schema_and_builtins() -> None:
    stored = {"metadata": {"type": "object", "properties": {"author": {"type": "string"}}}, "built_in_metadata": [{"key": "filename"}]}
    result = merge_document_parser_config(stored, {"metadata": {"required": ["author"]}})
    assert result == {**stored, "metadata": {**stored["metadata"], "required": ["author"]}}
    schema = turn2jsonschema(build_metadata_config(result))
    assert schema["required"] == ["author"]
    assert set(schema["properties"]) == {"author", "filename"}
    with pytest.raises(ValueError, match="merged metadata schema"):
        merge_document_parser_config({}, {"metadata": {"required": ["author"]}})
    with pytest.raises(ValueError, match="merged metadata schema"):
        merge_document_parser_config({}, {"metadata": {"unsupported": True}})


def test_legacy_delimiter_only_children_remain_enabled_after_partial_update() -> None:
    assert merge_document_parser_config({"children_delimiter": ";"}, {"children_delimiter": "!"}) == {
        "children_delimiter": "!",
        "enable_children": True,
        "parent_child": {"use_parent_child": True, "children_delimiter": "!"},
    }
    with pytest.raises(ValueError, match="explicit parent-child disable"):
        merge_document_parser_config({"children_delimiter": ";"}, {"children_delimiter": ""})
