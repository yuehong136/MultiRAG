"""Fail-closed U14 form schema and response normalization."""

from __future__ import annotations

import pytest

from api.identity.mcp_interactions.contracts import (
    InteractionErrorCode,
    InteractionStateError,
)
from api.identity.mcp_interactions.validation import (
    normalize_input_requests,
    normalize_input_responses,
    normalize_interaction_result,
    normalize_output_schema,
)


def _requests(property_name: str = "start") -> dict[str, object]:
    return {
        "leave-form": {
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": "Need leave dates",
                "requestedSchema": {
                    "type": "object",
                    "properties": {property_name: {"type": "string"}},
                    "required": [property_name],
                    "additionalProperties": False,
                },
            },
        }
    }


def test_form_schema_and_response_are_normalized() -> None:
    requests = normalize_input_requests(_requests(), max_payload_bytes=4096)

    responses = normalize_input_responses(
        input_requests=requests,
        input_responses={
            "leave-form": {
                "action": "accept",
                "content": {"start": "2026-09-01"},
            }
        },
        max_payload_bytes=4096,
    )

    assert responses["leave-form"]["action"] == "accept"


@pytest.mark.parametrize("property_name", ["password", "api_token", "payment_card_number"])
def test_secret_and_payment_collection_is_rejected(property_name: str) -> None:
    with pytest.raises(InteractionStateError) as raised:
        normalize_input_requests(
            _requests(property_name),
            max_payload_bytes=4096,
        )

    assert raised.value.code is InteractionErrorCode.PAYLOAD_INVALID


def test_response_must_match_request_ids_and_schema() -> None:
    requests = normalize_input_requests(_requests(), max_payload_bytes=4096)

    with pytest.raises(InteractionStateError) as raised:
        normalize_input_responses(
            input_requests=requests,
            input_responses={"other": {"action": "accept", "content": {"start": 42}}},
            max_payload_bytes=4096,
        )

    assert raised.value.code is InteractionErrorCode.RESPONSE_INVALID


def test_decline_and_cancel_cannot_smuggle_content() -> None:
    requests = normalize_input_requests(_requests(), max_payload_bytes=4096)

    with pytest.raises(InteractionStateError) as raised:
        normalize_input_responses(
            input_requests=requests,
            input_responses={"leave-form": {"action": "decline", "content": {"start": "x"}}},
            max_payload_bytes=4096,
        )

    assert raised.value.code is InteractionErrorCode.RESPONSE_INVALID


def test_structured_result_must_match_bounded_persisted_output_schema() -> None:
    schema = normalize_output_schema(
        {
            "type": "object",
            "properties": {"status": {"const": "prepared"}},
            "required": ["status"],
            "additionalProperties": False,
        },
        max_payload_bytes=4096,
    )

    assert normalize_interaction_result(
        {"status": "prepared"},
        output_schema=schema,
        max_payload_bytes=4096,
    ) == {"status": "prepared"}
    with pytest.raises(InteractionStateError) as raised:
        normalize_interaction_result(
            {"status": "executed"},
            output_schema=schema,
            max_payload_bytes=4096,
        )

    assert raised.value.code is InteractionErrorCode.PAYLOAD_INVALID


def test_output_schema_rejects_remote_references() -> None:
    with pytest.raises(InteractionStateError) as raised:
        normalize_output_schema(
            {"$ref": "https://schemas.example/result.json"},
            max_payload_bytes=4096,
        )

    assert raised.value.code is InteractionErrorCode.PAYLOAD_INVALID
