"""Security contracts for native Channel interaction form projection."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from api.channel_execution.interaction_forms import build_form_projection, decode_form_submission
from api.identity.mcp_interactions.contracts import InteractionErrorCode, InteractionStateError


def _input_requests() -> dict[str, Any]:
    return {
        "request-secret-id": {
            "method": "elicitation/create",
            "requestState": {"delegated_token": "must-not-leak"},
            "arguments": {"api_key": "must-not-leak"},
            "params": {
                "mode": "form",
                "message": "请补充审批信息",
                "requestedSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["internal_reason", "internal_tags"],
                    "properties": {
                        "internal_reason": {
                            "type": "string",
                            "title": "原因",
                            "minLength": 2,
                            "maxLength": 20,
                        },
                        "internal_count": {
                            "type": "integer",
                            "title": "数量",
                            "minimum": 1,
                            "maximum": 9,
                        },
                        "internal_confirmed": {
                            "type": "boolean",
                            "title": "确认",
                        },
                        "internal_date": {
                            "type": "string",
                            "title": "日期",
                            "format": "date",
                        },
                        "internal_level": {
                            "type": "string",
                            "title": "级别",
                            "enum": ["raw-secret-low", "raw-secret-high"],
                            "enumNames": ["低", "高"],
                        },
                        "internal_tags": {
                            "type": "array",
                            "title": "标签",
                            "items": {
                                "type": "string",
                                "enum": ["raw-secret-a", "raw-secret-b", "raw-secret-c"],
                                "enumNames": ["甲", "乙", "丙"],
                            },
                            "minItems": 1,
                            "maxItems": 3,
                            "uniqueItems": True,
                        },
                    },
                },
            },
        }
    }


def _field_id(mapping: dict[str, Any], property_name: str) -> str:
    fields = mapping["requests"]["request-secret-id"]
    return next(field_id for field_id, spec in fields.items() if spec["name"] == property_name)


def _option_id(mapping: dict[str, Any], property_name: str, raw_value: object) -> str:
    spec = mapping["requests"]["request-secret-id"][_field_id(mapping, property_name)]
    return next(option_id for option_id, value in spec["options"].items() if value == raw_value)


def test_projection_uses_revision_bound_opaque_ids_and_omits_private_envelope() -> None:
    projection, mapping = build_form_projection(
        interaction_id="interaction-1",
        revision=3,
        input_requests=_input_requests(),
    )
    repeated, _ = build_form_projection(
        interaction_id="interaction-1",
        revision=3,
        input_requests=_input_requests(),
    )
    next_revision, _ = build_form_projection(
        interaction_id="interaction-1",
        revision=4,
        input_requests=_input_requests(),
    )

    public = projection.model_dump_json()
    assert projection == repeated
    assert {field.name for field in projection.fields}.isdisjoint({field.name for field in next_revision.fields})
    assert all(field.name.startswith("f_") and len(field.name) == 26 for field in projection.fields)
    assert all(option.value.startswith("o_") for field in projection.fields for option in field.options)
    for private_value in (
        "request-secret-id",
        "internal_reason",
        "internal_tags",
        "raw-secret-low",
        "raw-secret-high",
        "raw-secret-a",
        "delegated_token",
        "api_key",
        "must-not-leak",
    ):
        assert private_value not in public

    assert _field_id(mapping, "internal_reason").startswith("f_")
    assert _option_id(mapping, "internal_level", "raw-secret-high").startswith("o_")


def test_decode_accepts_required_list_and_normalizes_allowlisted_values() -> None:
    _, mapping = build_form_projection(
        interaction_id="interaction-1",
        revision=3,
        input_requests=_input_requests(),
    )
    reason = _field_id(mapping, "internal_reason")
    count = _field_id(mapping, "internal_count")
    confirmed = _field_id(mapping, "internal_confirmed")
    date = _field_id(mapping, "internal_date")
    level = _field_id(mapping, "internal_level")
    tags = _field_id(mapping, "internal_tags")

    decoded = decode_form_submission(
        mapping=mapping,
        action="accept",
        form_value={
            reason: "批准上线",
            count: "7",
            confirmed: _option_id(mapping, "internal_confirmed", True),
            date: "2026-08-23",
            level: _option_id(mapping, "internal_level", "raw-secret-high"),
            tags: [
                _option_id(mapping, "internal_tags", "raw-secret-a"),
                _option_id(mapping, "internal_tags", "raw-secret-b"),
            ],
        },
    )

    assert decoded == {
        "request-secret-id": {
            "action": "accept",
            "content": {
                "internal_reason": "批准上线",
                "internal_count": 7,
                "internal_confirmed": True,
                "internal_date": "2026-08-23",
                "internal_level": "raw-secret-high",
                "internal_tags": ["raw-secret-a", "raw-secret-b"],
            },
        }
    }


@pytest.mark.parametrize(
    ("form_value", "expected_code"),
    [
        ({}, InteractionErrorCode.RESPONSE_INVALID),
        ({"attacker_field": "value"}, InteractionErrorCode.PAYLOAD_INVALID),
    ],
)
def test_decode_rejects_missing_required_or_unknown_fields(
    form_value: dict[str, Any],
    expected_code: InteractionErrorCode,
) -> None:
    _, mapping = build_form_projection(
        interaction_id="interaction-1",
        revision=3,
        input_requests=_input_requests(),
    )

    with pytest.raises(InteractionStateError) as exc_info:
        decode_form_submission(mapping=mapping, action="accept", form_value=form_value)

    assert exc_info.value.code is expected_code


def test_decline_and_cancel_never_accept_form_values() -> None:
    _, mapping = build_form_projection(
        interaction_id="interaction-1",
        revision=3,
        input_requests=_input_requests(),
    )

    assert decode_form_submission(mapping=mapping, action="decline", form_value={}) == {"request-secret-id": {"action": "decline"}}
    with pytest.raises(InteractionStateError) as exc_info:
        decode_form_submission(
            mapping=mapping,
            action="cancel",
            form_value={_field_id(mapping, "internal_reason"): "ignored"},
        )
    assert exc_info.value.code is InteractionErrorCode.PAYLOAD_INVALID


@pytest.mark.parametrize(
    ("property_name", "value"),
    [
        ("internal_count", 1.9),
        ("internal_date", "2026-02-31"),
    ],
    ids=("integer-float-truncation", "invalid-calendar-date"),
)
def test_decode_rejects_values_that_only_look_compatible(
    property_name: str,
    value: object,
) -> None:
    _, mapping = build_form_projection(
        interaction_id="interaction-1",
        revision=3,
        input_requests=_input_requests(),
    )
    form_value: dict[str, Any] = {
        _field_id(mapping, "internal_reason"): "批准上线",
        _field_id(mapping, "internal_tags"): [_option_id(mapping, "internal_tags", "raw-secret-a")],
        _field_id(mapping, property_name): value,
    }

    with pytest.raises(InteractionStateError) as exc_info:
        decode_form_submission(mapping=mapping, action="accept", form_value=form_value)

    assert exc_info.value.code is InteractionErrorCode.RESPONSE_INVALID


def test_decode_rejects_duplicate_multi_select_options() -> None:
    _, mapping = build_form_projection(
        interaction_id="interaction-1",
        revision=3,
        input_requests=_input_requests(),
    )
    option = _option_id(mapping, "internal_tags", "raw-secret-a")

    with pytest.raises(InteractionStateError) as exc_info:
        decode_form_submission(
            mapping=mapping,
            action="accept",
            form_value={
                _field_id(mapping, "internal_reason"): "批准上线",
                _field_id(mapping, "internal_tags"): [option, option],
            },
        )

    assert exc_info.value.code is InteractionErrorCode.RESPONSE_INVALID


@pytest.mark.parametrize(
    "mutate",
    [
        lambda schema: schema.update({"$ref": "https://attacker.invalid/schema.json"}),
        lambda schema: schema.update({"allOf": []}),
        lambda schema: schema["properties"].update({"nested": {"type": "object"}}),
        lambda schema: schema["properties"].update({"remote": {"type": "string", "format": "uri"}}),
        lambda schema: schema["properties"].update({"patterned": {"type": "string", "pattern": ".*"}}),
        lambda schema: schema["properties"].update({"nested_list": {"type": "array", "items": {"type": "object"}}}),
        lambda schema: schema["properties"].update({"boolean_with_number_constraint": {"type": "boolean", "minimum": 0}}),
        lambda schema: schema["properties"].update({"wrong_enum_type": {"type": "string", "enum": [1, 2]}}),
        lambda schema: schema["properties"]["internal_tags"].update({"minItems": 2}),
        lambda schema: schema["properties"]["internal_tags"].update({"maxItems": 2}),
    ],
    ids=(
        "remote-ref",
        "composition",
        "nested-object",
        "unsupported-format",
        "pattern",
        "nested-list",
        "boolean-number-constraint",
        "wrong-enum-type",
        "unenforceable-multi-minimum",
        "unenforceable-multi-maximum",
    ),
)
def test_projection_rejects_non_shallow_or_remote_schema_shapes(mutate: Any) -> None:
    requests = deepcopy(_input_requests())
    schema = requests["request-secret-id"]["params"]["requestedSchema"]
    mutate(schema)

    with pytest.raises(InteractionStateError) as exc_info:
        build_form_projection(
            interaction_id="interaction-1",
            revision=3,
            input_requests=requests,
        )

    assert exc_info.value.code is InteractionErrorCode.PAYLOAD_INVALID
