"""Fail-closed normalization for human-renderable MCP input rounds."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from api.identity.mcp_interactions.contracts import (
    InteractionErrorCode,
    InteractionStateError,
)

_FORBIDDEN_FIELD_PARTS = (
    "api_key",
    "apikey",
    "card_number",
    "credit_card",
    "cvv",
    "oauth",
    "password",
    "payment",
    "secret",
    "token",
)
_ACTIONS = frozenset({"accept", "decline", "cancel"})


def _payload_size(value: object) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
    except (TypeError, ValueError) as exc:
        raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID) from exc


def _assert_safe_schema(schema: object) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID) from exc
    pending: list[tuple[str, object]] = [("", schema)]
    while pending:
        field_name, node = pending.pop()
        if any(part in field_name.casefold() for part in _FORBIDDEN_FIELD_PARTS):
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
        if not isinstance(node, dict):
            continue
        if str(node.get("format", "")).casefold() == "password":
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
        properties = node.get("properties", {})
        if properties:
            if not isinstance(properties, dict):
                raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
            pending.extend((str(name), child) for name, child in properties.items())
        items = node.get("items")
        if items is not None:
            pending.append((field_name, items))
    return schema


def normalize_input_requests(
    input_requests: Mapping[str, Any],
    *,
    max_payload_bytes: int,
) -> dict[str, Any]:
    """Accept only bounded form elicitation that a Host can validate safely."""

    normalized = dict(input_requests)
    if not normalized or _payload_size(normalized) > max_payload_bytes:
        raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
    for request_id, raw_request in normalized.items():
        if not request_id.strip() or not isinstance(raw_request, dict):
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
        params = raw_request.get("params")
        if raw_request.get("method") != "elicitation/create" or not isinstance(params, dict):
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
        mode = params.get("mode", "form")
        if mode != "form":
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
        _assert_safe_schema(params.get("requestedSchema"))
    return normalized


def interaction_schema_digest(input_requests: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(input_requests),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(("multirag.mcp-interaction.schema.v1\x00" + canonical).encode()).hexdigest()


def normalize_output_schema(
    output_schema: Mapping[str, Any] | None,
    *,
    max_payload_bytes: int,
) -> dict[str, Any] | None:
    """Validate and bound a tool output contract without resolving remote refs."""

    if output_schema is None:
        return None
    normalized = dict(output_schema)
    if not normalized or _payload_size(normalized) > max_payload_bytes:
        raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
    try:
        Draft202012Validator.check_schema(normalized)
    except SchemaError as exc:
        raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID) from exc
    pending: list[object] = [normalized]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            reference = node.get("$ref")
            if reference is not None and (not isinstance(reference, str) or not reference.startswith("#")):
                raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return normalized


def output_schema_digest(output_schema: Mapping[str, Any] | None) -> str | None:
    if output_schema is None:
        return None
    canonical = json.dumps(
        dict(output_schema),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(
        ("multirag.mcp-interaction.output-schema.v1\x00" + canonical).encode(),
    ).hexdigest()


def normalize_interaction_result(
    result: object,
    *,
    output_schema: Mapping[str, Any] | None,
    max_payload_bytes: int,
) -> object:
    """Bound a JSON-compatible result and enforce the persisted tool schema."""

    if _payload_size(result) > max_payload_bytes:
        raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
    schema = normalize_output_schema(
        output_schema,
        max_payload_bytes=max_payload_bytes,
    )
    if schema is not None:
        try:
            Draft202012Validator(schema).validate(result)
        except ValidationError as exc:
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID) from exc
    return result


def normalize_input_responses(
    *,
    input_requests: Mapping[str, Any],
    input_responses: Mapping[str, Any],
    max_payload_bytes: int,
) -> dict[str, Any]:
    normalized = dict(input_responses)
    if set(normalized) != set(input_requests) or _payload_size(normalized) > max_payload_bytes:
        raise InteractionStateError(InteractionErrorCode.RESPONSE_INVALID)
    for request_id, raw_response in normalized.items():
        if not isinstance(raw_response, dict) or set(raw_response) - {"action", "content"}:
            raise InteractionStateError(InteractionErrorCode.RESPONSE_INVALID)
        action = raw_response.get("action")
        if action not in _ACTIONS:
            raise InteractionStateError(InteractionErrorCode.RESPONSE_INVALID)
        if action == "accept":
            content = raw_response.get("content")
            request = input_requests[request_id]
            params = request.get("params") if isinstance(request, dict) else None
            schema = params.get("requestedSchema") if isinstance(params, dict) else None
            try:
                Draft202012Validator(_assert_safe_schema(schema)).validate(content)
            except ValidationError as exc:
                raise InteractionStateError(InteractionErrorCode.RESPONSE_INVALID) from exc
        elif "content" in raw_response:
            raise InteractionStateError(InteractionErrorCode.RESPONSE_INVALID)
    return normalized


__all__ = [
    "interaction_schema_digest",
    "normalize_input_requests",
    "normalize_input_responses",
    "normalize_interaction_result",
    "normalize_output_schema",
    "output_schema_digest",
]
