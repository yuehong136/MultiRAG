"""Strict transport-neutral projection for native Channel interaction forms."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from api.identity.mcp_interactions.contracts import InteractionErrorCode, InteractionStateError

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_REQUESTS = 4
_MAX_FIELDS = 12
_MAX_OPTIONS = 20
_MAX_LABEL = 80
_MAX_MESSAGE = 240
_MAX_TEXT = 1000
_ROOT_KEYS = frozenset({"$schema", "type", "title", "description", "properties", "required", "additionalProperties"})
_COMMON_FIELD_KEYS = frozenset({"type", "title", "description", "default"})
_STRING_FIELD_KEYS = _COMMON_FIELD_KEYS | {"enum", "enumNames", "format", "minLength", "maxLength", "pattern"}
_NUMBER_FIELD_KEYS = _COMMON_FIELD_KEYS | {"minimum", "maximum"}
_ARRAY_FIELD_KEYS = _COMMON_FIELD_KEYS | {"items", "minItems", "maxItems", "uniqueItems"}


class InteractionFormOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=_MAX_LABEL)


class InteractionFormField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    kind: Literal["text", "number", "boolean", "date", "select", "multi_select"]
    label: str = Field(min_length=1, max_length=_MAX_LABEL)
    required: bool = False
    options: tuple[InteractionFormOption, ...] = ()
    min_length: int | None = Field(default=None, ge=0, le=_MAX_TEXT)
    max_length: int | None = Field(default=None, ge=1, le=_MAX_TEXT)
    minimum: float | None = None
    maximum: float | None = None


class InteractionFormProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message: str = Field(min_length=1, max_length=_MAX_MESSAGE)
    fields: tuple[InteractionFormField, ...] = Field(min_length=1, max_length=_MAX_FIELDS)


def _reject() -> None:
    raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)


def _bounded_plain(value: object, *, fallback: str, limit: int) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > limit:
        return fallback
    return normalized


def _opaque(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode()
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return f"{prefix}_{digest.hexdigest()[:24]}"


def _integer(value: object, *, default: int | None = None, upper: int = _MAX_TEXT) -> int | None:
    if value is None:
        return default
    if type(value) is not int or value < 0 or value > upper:
        _reject()
    return value


def _number(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(float(value)):
        _reject()
    return float(value)


def _options(
    *,
    interaction_id: str,
    revision: int,
    request_id: str,
    property_name: str,
    values: object,
    labels: object,
    value_type: Literal["string", "boolean"],
) -> tuple[tuple[InteractionFormOption, ...], dict[str, object]]:
    if not isinstance(values, list) or not 1 <= len(values) <= _MAX_OPTIONS or len({repr(value) for value in values}) != len(values):
        _reject()
    if labels is not None and (not isinstance(labels, list) or len(labels) != len(values)):
        _reject()
    public: list[InteractionFormOption] = []
    private: dict[str, object] = {}
    for index, value in enumerate(values):
        if (value_type == "string" and not isinstance(value, str)) or (value_type == "boolean" and type(value) is not bool):
            _reject()
        raw_label = labels[index] if isinstance(labels, list) else value
        label = _bounded_plain(raw_label, fallback=f"选项 {index + 1}", limit=_MAX_LABEL)
        option_id = _opaque(
            "o",
            interaction_id,
            str(revision),
            request_id,
            property_name,
            str(index),
        )
        public.append(InteractionFormOption(value=option_id, label=label))
        private[option_id] = value
    return tuple(public), private


def build_form_projection(
    *,
    interaction_id: str,
    revision: int,
    input_requests: Mapping[str, Any],
) -> tuple[InteractionFormProjection, dict[str, Any]]:
    """Convert MCP form requests to an allowlisted DTO and encrypted server map."""

    if not 1 <= len(input_requests) <= _MAX_REQUESTS:
        _reject()
    public_fields: list[InteractionFormField] = []
    private_requests: dict[str, Any] = {}
    messages: list[str] = []
    for request_index, (request_id, request) in enumerate(input_requests.items()):
        if not isinstance(request_id, str) or not request_id.strip() or not isinstance(request, Mapping):
            _reject()
        params = request.get("params")
        if request.get("method") != "elicitation/create" or not isinstance(params, Mapping) or params.get("mode", "form") != "form":
            _reject()
        schema = params.get("requestedSchema")
        if not isinstance(schema, Mapping) or set(schema) - _ROOT_KEYS or schema.get("type") != "object":
            _reject()
        if schema.get("additionalProperties") not in {None, False}:
            _reject()
        properties = schema.get("properties")
        required = schema.get("required", [])
        if not isinstance(properties, Mapping) or not properties or not isinstance(required, list) or any(not isinstance(name, str) for name in required):
            _reject()
        if len(set(required)) != len(required) or not set(required).issubset(properties):
            _reject()
        message = _bounded_plain(params.get("message"), fallback="请补充以下信息", limit=_MAX_MESSAGE)
        messages.append(message)
        request_fields: dict[str, Any] = {}
        for property_index, (property_name, raw_field) in enumerate(properties.items()):
            if len(public_fields) >= _MAX_FIELDS or not isinstance(property_name, str) or not property_name.strip() or not isinstance(raw_field, Mapping):
                _reject()
            field_type = raw_field.get("type")
            allowed_keys = (
                _STRING_FIELD_KEYS
                if field_type == "string"
                else _NUMBER_FIELD_KEYS
                if field_type in {"integer", "number"}
                else _COMMON_FIELD_KEYS
                if field_type == "boolean"
                else _ARRAY_FIELD_KEYS
                if field_type == "array"
                else frozenset()
            )
            if not allowed_keys or set(raw_field) - allowed_keys:
                _reject()
            field_id = _opaque(
                "f",
                interaction_id,
                str(revision),
                request_id,
                property_name,
            )
            label = _bounded_plain(
                raw_field.get("title"),
                fallback=f"字段 {request_index + 1}.{property_index + 1}",
                limit=_MAX_LABEL,
            )
            kind: Literal["text", "number", "boolean", "date", "select", "multi_select"]
            options: tuple[InteractionFormOption, ...] = ()
            option_map: dict[str, object] = {}
            minimum: float | None = None
            maximum: float | None = None
            min_length: int | None = None
            max_length: int | None = None
            if field_type == "string":
                enum = raw_field.get("enum")
                if enum is not None:
                    kind = "select"
                    options, option_map = _options(
                        interaction_id=interaction_id,
                        revision=revision,
                        request_id=request_id,
                        property_name=property_name,
                        values=enum,
                        labels=raw_field.get("enumNames"),
                        value_type="string",
                    )
                elif raw_field.get("format") == "date":
                    kind = "date"
                elif raw_field.get("format") is None:
                    kind = "text"
                else:
                    _reject()
                if raw_field.get("pattern") is not None:
                    _reject()
                min_length = _integer(raw_field.get("minLength"), default=0)
                max_length = _integer(raw_field.get("maxLength"), default=_MAX_TEXT)
                if max_length is None or min_length is None or min_length > max_length:
                    _reject()
            elif field_type in {"integer", "number"}:
                kind = "number"
                minimum = _number(raw_field.get("minimum"))
                maximum = _number(raw_field.get("maximum"))
                if minimum is not None and maximum is not None and minimum > maximum:
                    _reject()
            elif field_type == "boolean":
                kind = "boolean"
                options, option_map = _options(
                    interaction_id=interaction_id,
                    revision=revision,
                    request_id=request_id,
                    property_name=property_name,
                    values=[True, False],
                    labels=["是", "否"],
                    value_type="boolean",
                )
            else:
                items = raw_field.get("items")
                if not isinstance(items, Mapping) or set(items) - {"type", "enum", "enumNames"} or items.get("type") != "string":
                    _reject()
                kind = "multi_select"
                options, option_map = _options(
                    interaction_id=interaction_id,
                    revision=revision,
                    request_id=request_id,
                    property_name=property_name,
                    values=items.get("enum"),
                    labels=items.get("enumNames"),
                    value_type="string",
                )
                min_length = _integer(raw_field.get("minItems"), default=0, upper=_MAX_OPTIONS)
                max_length = _integer(raw_field.get("maxItems"), default=len(options), upper=_MAX_OPTIONS)
                if raw_field.get("uniqueItems") not in {None, True} or min_length is None or max_length is None or min_length > 1 or max_length != len(options):
                    _reject()
            public_fields.append(
                InteractionFormField(
                    name=field_id,
                    kind=kind,
                    label=label,
                    required=property_name in required,
                    options=options,
                    min_length=min_length,
                    max_length=max_length,
                    minimum=minimum,
                    maximum=maximum,
                )
            )
            request_fields[field_id] = {
                "name": property_name,
                "type": field_type,
                "kind": kind,
                "required": property_name in required,
                "options": option_map,
                "min": min_length if kind in {"text", "multi_select"} else minimum,
                "max": max_length if kind in {"text", "multi_select"} else maximum,
            }
        private_requests[request_id] = request_fields
    projection = InteractionFormProjection(
        message=" / ".join(dict.fromkeys(messages))[:_MAX_MESSAGE],
        fields=tuple(public_fields),
    )
    return projection, {"requests": private_requests}


def decode_form_submission(
    *,
    mapping: Mapping[str, Any],
    action: Literal["accept", "decline", "cancel"],
    form_value: Mapping[str, Any],
) -> dict[str, Any]:
    """Map opaque Provider component values back to MCP inputResponses."""

    raw_requests = mapping.get("requests")
    if not isinstance(raw_requests, Mapping):
        _reject()
    if action != "accept":
        if form_value:
            _reject()
        return {str(request_id): {"action": action} for request_id in raw_requests}
    known_fields = {field_id for fields in raw_requests.values() if isinstance(fields, Mapping) for field_id in fields}
    if set(form_value) - known_fields:
        _reject()
    responses: dict[str, Any] = {}
    for request_id, raw_fields in raw_requests.items():
        if not isinstance(request_id, str) or not isinstance(raw_fields, Mapping):
            _reject()
        content: dict[str, Any] = {}
        for field_id, raw_spec in raw_fields.items():
            if not isinstance(field_id, str) or not isinstance(raw_spec, Mapping):
                _reject()
            required = raw_spec.get("required") is True
            if field_id not in form_value or form_value[field_id] is None or form_value[field_id] == "":
                if required:
                    raise InteractionStateError(InteractionErrorCode.RESPONSE_INVALID)
                continue
            value = form_value[field_id]
            kind = raw_spec.get("kind")
            options = raw_spec.get("options")
            try:
                if kind in {"select", "boolean"}:
                    if not isinstance(value, str) or not isinstance(options, Mapping) or value not in options:
                        raise ValueError
                    normalized = options[value]
                elif kind == "multi_select":
                    if not isinstance(value, list) or not isinstance(options, Mapping) or len(value) != len(set(value)) or any(not isinstance(item, str) or item not in options for item in value):
                        raise ValueError
                    normalized = [options[item] for item in value]
                    minimum = raw_spec.get("min")
                    maximum = raw_spec.get("max")
                    if not isinstance(minimum, int) or not isinstance(maximum, int) or not minimum <= len(normalized) <= maximum:
                        raise ValueError
                elif kind == "number":
                    if isinstance(value, bool) or not isinstance(value, str | int | float):
                        raise ValueError
                    field_type = raw_spec.get("type")
                    if field_type == "integer":
                        if type(value) is int:
                            normalized = value
                        elif isinstance(value, str) and re.fullmatch(
                            r"-?(?:0|[1-9]\d*)",
                            value,
                        ):
                            normalized = int(value)
                        else:
                            raise ValueError
                    else:
                        normalized = float(value)
                    if isinstance(normalized, float) and not math.isfinite(normalized):
                        raise ValueError
                    minimum = raw_spec.get("min")
                    maximum = raw_spec.get("max")
                    if (minimum is not None and normalized < minimum) or (maximum is not None and normalized > maximum):
                        raise ValueError
                elif kind in {"text", "date"}:
                    if not isinstance(value, str):
                        raise ValueError
                    if kind == "date":
                        if _DATE.fullmatch(value) is None:
                            raise ValueError
                        date.fromisoformat(value)
                    minimum = raw_spec.get("min") or 0
                    maximum = raw_spec.get("max") or _MAX_TEXT
                    if not minimum <= len(value) <= maximum:
                        raise ValueError
                    normalized = value
                else:
                    raise ValueError
            except (TypeError, ValueError, OverflowError) as exc:
                raise InteractionStateError(InteractionErrorCode.RESPONSE_INVALID) from exc
            property_name = raw_spec.get("name")
            if not isinstance(property_name, str) or not property_name:
                _reject()
            content[property_name] = normalized
        responses[request_id] = {"action": "accept", "content": content}
    return responses


__all__ = [
    "InteractionFormField",
    "InteractionFormOption",
    "InteractionFormProjection",
    "build_form_projection",
    "decode_form_submission",
]
