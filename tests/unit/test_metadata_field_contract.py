from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from api.apps.restful_apis.dataset_api import CreateDatasetRequest, UpdateDatasetRequest
from api.utils.document_parser_config import merge_document_parser_config
from common.metadata_config import MetadataConfig, apply_metadata_config
from common.metadata_utils import build_metadata_config, turn2jsonschema


def test_typed_fields_reach_extraction_without_losing_examples() -> None:
    fields = [
        {"key": "score", "type": "number", "enum": ["0", "1.25", 9007199254740993]},
        {"key": "tags", "type": "list", "enum": ["A", "B"]},
        {"name": "year", "type": "time", "examples": ["2026"], "restrict_values": False},
        {"name": "category", "type": "string", "examples": ["book"], "restrict_values": True},
    ]
    original = deepcopy(fields)
    config = MetadataConfig.model_validate({"metadata": fields, "built_in_metadata": [{"key": "source", "type": "string"}]})
    schema = turn2jsonschema(build_metadata_config(config.model_dump(exclude_unset=True)))
    assert schema["properties"] == {
        "score": {"description": "", "type": "number", "enum": [0, 1.25, 9007199254740993]},
        "tags": {"description": "", "type": "array", "items": {"type": "string", "enum": ["A", "B"]}},
        "year": {"description": "", "type": "string", "examples": ["2026"]},
        "category": {"description": "", "type": "string", "enum": ["book"]},
        "source": {"description": "", "type": "string"},
    }
    assert fields == original


@pytest.mark.parametrize("bad", ["nan", "inf", "", "not-number", True])
def test_invalid_numeric_enums_rejected(bad: Any) -> None:
    with pytest.raises(ValidationError):
        MetadataConfig.model_validate({"metadata": [{"key": "score", "type": "number", "enum": [bad]}]})


@pytest.mark.parametrize(
    "config",
    [
        {"metadata": None},
        {"built_in_metadata": None},
        {"enabled": None},
        {"fields": None},
        {"enabled": "false"},
        {"enabled": False, "unknown": []},
        {"metadata": [], "fields": [{"name": "x"}]},
        {"metadata": [{"key": "a", "name": "b"}]},
        {"metadata": [{"key": "x", "type": "unknown"}]},
    ],
)
def test_invalid_or_ambiguous_envelopes_rejected(config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MetadataConfig.model_validate(config)


def test_new_partial_config_preserves_switch_schema_and_unknown_settings() -> None:
    stored = {
        "enable_metadata": False,
        "metadata": {"type": "object", "properties": {"year": {"type": "integer"}}, "required": ["year"]},
        "built_in_metadata": [{"key": "source"}],
        "future": {"unknown": 1},
    }
    original = deepcopy(stored)
    assert apply_metadata_config(stored, {"built_in_metadata": []}) == {**stored, "built_in_metadata": []}
    assert apply_metadata_config(stored, {"metadata": []}) == {**stored, "metadata": []}
    assert apply_metadata_config(stored, {"metadata": [], "enabled": True})["enable_metadata"] is True
    assert stored == original
    assert apply_metadata_config(stored, {}) == stored


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    "metadata",
    [
        [{"name": "author", "examples": ["Ada"], "future": {"keep": True}}],
        {"type": "object", "properties": {"year": {"type": "integer"}}, "required": ["year"]},
    ],
)
def test_enabled_only_preserves_definitions_without_mutating_inputs(enabled: bool, metadata: list[dict[str, Any]] | dict[str, Any]) -> None:
    stored = {"metadata": metadata, "built_in_metadata": [{"key": "source"}], "future": {"keep": [1]}}
    original = deepcopy(stored)
    result = apply_metadata_config(stored, {"enabled": enabled})
    assert result == {**stored, "enable_metadata": enabled}
    result["built_in_metadata"][0]["key"] = "changed"
    result["future"]["keep"].append(2)
    assert stored == original


@pytest.mark.parametrize("config", [{"enabled": None}, {"fields": None}, {"enabled": 0}, {"enabled": False, "unknown": []}])
def test_invalid_toggle_leaves_stored_config_untouched(config: dict[str, Any]) -> None:
    stored = {"enable_metadata": False, "metadata": [{"key": "author"}], "built_in_metadata": [{"key": "source"}]}
    original = deepcopy(stored)
    with pytest.raises(ValidationError):
        apply_metadata_config(stored, config)
    assert stored == original


@pytest.mark.parametrize("config", [{"fields": []}, {"fields": [{"name": "author"}], "enabled": False}, {"metadata": [], "fields": []}])
def test_legacy_fields_envelopes_are_rejected(config: dict[str, Any]) -> None:
    stored = {"enable_metadata": False, "metadata": [{"key": "author"}]}
    with pytest.raises(ValidationError):
        apply_metadata_config(stored, config)
    assert stored == {"enable_metadata": False, "metadata": [{"key": "author"}]}
    with pytest.raises(ValidationError):
        CreateDatasetRequest.model_validate({"name": "sample", "auto_metadata_config": config})
    with pytest.raises(ValidationError):
        UpdateDatasetRequest.model_validate({"auto_metadata_config": config})


def test_enabled_only_does_not_add_omitted_definitions() -> None:
    assert apply_metadata_config({"future": {"keep": 1}}, {"enabled": False}) == {"future": {"keep": 1}, "enable_metadata": False}


@pytest.mark.parametrize("key", ["auto_metadata_config", "parser_config"])
def test_dataset_create_and_update_preserve_explicit_fields(key: str) -> None:
    config = {"metadata": [{"key": "score", "type": "number"}], "built_in_metadata": [{"key": "source", "type": "string"}]}
    create = CreateDatasetRequest.model_validate({"name": "sample", key: config}).model_dump(exclude_unset=True)
    update = UpdateDatasetRequest.model_validate({key: config}).model_dump(exclude_unset=True)
    assert create[key] == update[key] == config


def test_document_patch_accepts_types_without_filling_defaults_or_changing_unknowns() -> None:
    stored = {"enable_metadata": False, "metadata": [{"key": "old"}], "built_in_metadata": [{"key": "source"}], "future": {"unknown": 1}}
    fields = [{"key": "score", "type": "number", "enum": ["1.5"]}]
    assert merge_document_parser_config(stored, {"metadata": fields}) == {**stored, "metadata": fields}
    assert merge_document_parser_config(stored, {"metadata": []}) == {**stored, "metadata": []}
    assert merge_document_parser_config(stored, {}) == stored
    with pytest.raises(ValidationError):
        merge_document_parser_config(stored, {"metadata": [{"key": "x", "unsupported": 1}]})


@pytest.mark.parametrize("items", [False, True])
@pytest.mark.parametrize("values", [None, [], ["one"]])
def test_boolean_item_schemas_remain_readable_and_keep_constraints(items: bool, values: list[str] | None) -> None:
    from jsonschema import Draft202012Validator

    fields: list[dict[str, Any]] = [{"key": "values", "type": "list", "items": items, "future": {"keep": True}}, {"key": "b", "type": "string"}]
    if values is not None:
        fields[0]["enum"] = values
    before = deepcopy(fields)
    config = MetadataConfig.model_validate({"metadata": fields}).model_dump(exclude_unset=True)
    assert config["metadata"] == fields
    projected = turn2jsonschema(fields)
    Draft202012Validator.check_schema(projected)
    schema = projected["properties"]["values"]
    assert schema["items"] == ({"type": "string", "enum": values} if items and values else items)
    assert schema["future"] == {"keep": True}
    validator = Draft202012Validator(projected)
    assert validator.is_valid({"values": []})
    assert validator.is_valid({"values": ["one"]}) is items
    assert validator.is_valid({"values": ["two"]}) is (items and not bool(values))
    merged = merge_document_parser_config(config, {"metadata": {"properties": {"b": {"description": "Changed b"}}}})
    assert merged["metadata"]["properties"]["values"] == schema
    assert fields == before


@pytest.mark.parametrize("items", [None, "string", 0, 1.5, []])
def test_unsupported_list_item_shapes_are_rejected_before_storage(items: Any) -> None:
    with pytest.raises(ValidationError, match="object or boolean schema"):
        MetadataConfig.model_validate({"metadata": [{"key": "values", "type": "list", "items": items}]})
