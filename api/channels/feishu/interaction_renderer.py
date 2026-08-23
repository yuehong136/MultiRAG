"""Feishu Card JSON 2.0 renderer for an approved interaction projection."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol, TypeGuard, runtime_checkable

_MAX_FIELDS = 16
_MAX_OPTIONS = 20
_MAX_CARD_BYTES = 24_000
_MAX_MESSAGE_CHARS = 300
_MAX_LABEL_CHARS = 100
_MAX_FIELD_NAME_CHARS = 64
_MAX_OPTION_VALUE_CHARS = 64
_MAX_INPUT_CHARS = 1_000
_MAX_PROJECTED_TEXT_CHARS = 4_000
_MAX_OPAQUE_CHARS = 255

type FeishuFormFieldKind = Literal[
    "text",
    "number",
    "boolean",
    "date",
    "select",
    "multi_select",
]


class FeishuInteractionRenderError(ValueError):
    """Raised when a projection cannot use the native-form subset."""


@runtime_checkable
class FeishuFormOptionProjection(Protocol):
    """Transport-neutral option emitted by the Channel execution layer."""

    @property
    def value(self) -> str: ...

    @property
    def label(self) -> str: ...


@runtime_checkable
class FeishuFormFieldProjection(Protocol):
    """Structural field contract; no dependency on Channel execution code."""

    @property
    def name(self) -> str: ...

    @property
    def kind(self) -> FeishuFormFieldKind: ...

    @property
    def label(self) -> str: ...

    @property
    def required(self) -> bool: ...

    @property
    def options(self) -> Sequence[FeishuFormOptionProjection]: ...

    @property
    def min_length(self) -> int | None: ...

    @property
    def max_length(self) -> int | None: ...

    @property
    def minimum(self) -> float | None: ...

    @property
    def maximum(self) -> float | None: ...


@runtime_checkable
class FeishuFormProjection(Protocol):
    """Structural presentation contract accepted by the Feishu adapter."""

    @property
    def message(self) -> str: ...

    @property
    def fields(self) -> Sequence[FeishuFormFieldProjection]: ...


@runtime_checkable
class FeishuTerminalProjection(Protocol):
    @property
    def state(
        self,
    ) -> Literal[
        "completed",
        "declined",
        "cancelled",
        "expired",
        "failed",
    ]: ...

    @property
    def message(self) -> str: ...


@dataclass(frozen=True, slots=True)
class RenderedFeishuInteractionForm:
    """Complete native form and its already-opaque component names."""

    card_json: str = field(repr=False)
    field_names: tuple[str, ...]


@runtime_checkable
class FeishuInteractionCardTransport(Protocol):
    """Whole-card operation required after a reply stream is finished."""

    async def update_card(self, message_id: str, card_json: str) -> None: ...


class FeishuInteractionRenderer:
    """Render and publish one approved native form projection."""

    def __init__(self, transport: FeishuInteractionCardTransport) -> None:
        self._transport = transport

    def render(
        self,
        *,
        action_id: str,
        action_nonce: str,
        revision: int,
        expires_at: datetime,
        projection: FeishuFormProjection,
    ) -> RenderedFeishuInteractionForm:
        return render_interaction_form(
            action_id=action_id,
            action_nonce=action_nonce,
            revision=revision,
            expires_at=expires_at,
            projection=projection,
        )

    async def present(
        self,
        *,
        reply_message_id: str,
        action_id: str,
        action_nonce: str,
        revision: int,
        expires_at: datetime,
        projection: FeishuFormProjection,
    ) -> RenderedFeishuInteractionForm:
        if not _is_bounded_text(reply_message_id, _MAX_OPAQUE_CHARS):
            raise FeishuInteractionRenderError("reply message ID is invalid")
        rendered = self.render(
            action_id=action_id,
            action_nonce=action_nonce,
            revision=revision,
            expires_at=expires_at,
            projection=projection,
        )
        await self._transport.update_card(reply_message_id, rendered.card_json)
        return rendered

    async def present_terminal(
        self,
        *,
        reply_message_id: str,
        projection: FeishuTerminalProjection,
    ) -> None:
        if not _is_bounded_text(reply_message_id, _MAX_OPAQUE_CHARS):
            raise FeishuInteractionRenderError("reply message ID is invalid")
        await self._transport.update_card(
            reply_message_id,
            render_interaction_terminal(projection),
        )


def render_interaction_form(
    *,
    action_id: str,
    action_nonce: str,
    revision: int,
    expires_at: datetime,
    projection: FeishuFormProjection,
) -> RenderedFeishuInteractionForm:
    """Map only an approved structural DTO to one Card JSON 2.0 form."""

    if not _is_bounded_text(action_id, _MAX_OPAQUE_CHARS):
        raise FeishuInteractionRenderError("action ID is invalid")
    if not _is_bounded_text(action_nonce, _MAX_OPAQUE_CHARS):
        raise FeishuInteractionRenderError("action nonce is invalid")
    if type(revision) is not int or not 1 <= revision <= 2**63 - 1:
        raise FeishuInteractionRenderError("revision is invalid")
    if type(expires_at) is not datetime or expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise FeishuInteractionRenderError("expiration is invalid")
    if not isinstance(projection, FeishuFormProjection):
        raise FeishuInteractionRenderError("form projection is invalid")
    message = _required_text(
        projection.message,
        max_chars=_MAX_MESSAGE_CHARS,
        field="projection message",
    )
    fields = tuple(projection.fields)
    if not 1 <= len(fields) <= _MAX_FIELDS:
        raise FeishuInteractionRenderError("native form field count is unsupported")

    form_elements: list[dict[str, object]] = [_plain_div(message)]
    field_names: list[str] = []
    for field_projection in fields:
        if not isinstance(field_projection, FeishuFormFieldProjection):
            raise FeishuInteractionRenderError("form field projection is invalid")
        if field_projection.name in field_names:
            raise FeishuInteractionRenderError("form field names must be unique")
        form_elements.extend(_render_field(field_projection))
        field_names.append(field_projection.name)

    expires_text = expires_at.astimezone(UTC).strftime("有效期至 %Y-%m-%d %H:%M UTC")
    form_elements.extend(
        [
            _plain_div(expires_text),
            {
                "tag": "button",
                "name": "interaction_submit",
                "text": {"tag": "plain_text", "content": "提交"},
                "type": "primary_filled",
                "width": "fill",
                "form_action_type": "submit",
                # form_action_type collects form_value. This callback behavior
                # carries only server-generated routing material.
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {
                            "action_id": action_id,
                            "nonce": action_nonce,
                            "revision": revision,
                            "action": "accept",
                        },
                    }
                ],
            },
        ]
    )
    card = {
        "schema": "2.0",
        "config": {
            "update_multi": False,
            "summary": {"content": "等待补充信息"},
        },
        "header": {
            "title": {"tag": "plain_text", "content": "需要补充信息"},
            "template": "blue",
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {
                    "tag": "form",
                    "name": "interaction_form",
                    "direction": "vertical",
                    "elements": form_elements,
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "取消"},
                    "type": "default",
                    "width": "fill",
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {
                                "action_id": action_id,
                                "nonce": action_nonce,
                                "revision": revision,
                                "action": "cancel",
                            },
                        }
                    ],
                },
            ],
        },
    }
    card_json = json.dumps(
        card,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    if len(card_json.encode()) > _MAX_CARD_BYTES:
        raise FeishuInteractionRenderError("native form card is too large")
    return RenderedFeishuInteractionForm(
        card_json=card_json,
        field_names=tuple(field_names),
    )


def render_interaction_terminal(
    projection: FeishuTerminalProjection,
) -> str:
    """Render a safe result page without reusable callback controls."""

    if not isinstance(projection, FeishuTerminalProjection):
        raise FeishuInteractionRenderError("terminal projection is invalid")
    states = {
        "completed": ("处理完成", "green"),
        "declined": ("已拒绝", "grey"),
        "cancelled": ("已取消", "grey"),
        "expired": ("已过期", "orange"),
        "failed": ("处理失败", "red"),
    }
    state = projection.state
    if state not in states:
        raise FeishuInteractionRenderError("terminal state is invalid")
    title, template = states[state]
    message = _required_text(
        projection.message,
        max_chars=_MAX_MESSAGE_CHARS,
        field="terminal message",
    )
    card_json = json.dumps(
        {
            "schema": "2.0",
            "config": {
                "update_multi": False,
                "summary": {"content": title},
            },
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "body": {
                "direction": "vertical",
                "elements": [_plain_div(message)],
            },
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    if len(card_json.encode()) > _MAX_CARD_BYTES:
        raise FeishuInteractionRenderError("terminal card is too large")
    return card_json


def _render_field(field_projection: FeishuFormFieldProjection) -> list[dict[str, object]]:
    name = _required_text(
        field_projection.name,
        max_chars=_MAX_FIELD_NAME_CHARS,
        field="field name",
    )
    label = _required_text(
        field_projection.label,
        max_chars=_MAX_LABEL_CHARS,
        field="field label",
    )
    if type(field_projection.required) is not bool:
        raise FeishuInteractionRenderError("field required flag is invalid")
    kind = field_projection.kind
    if kind == "text":
        _reject_options(field_projection.options)
        minimum, maximum = _text_bounds(field_projection)
        return [
            {
                "tag": "input",
                "name": name,
                "required": field_projection.required,
                "label": {
                    "tag": "plain_text",
                    "content": _required_label(label, field_projection.required),
                },
                "placeholder": _placeholder("请填写"),
                "input_type": "multiline_text" if maximum > 200 else "text",
                # Card JSON 2.0 input is capped at 1000 characters. The Host
                # still applies the projection's stricter server-side bounds.
                "max_length": min(maximum, _MAX_INPUT_CHARS),
            }
        ]
    if kind == "number":
        _reject_options(field_projection.options)
        _validate_number_bounds(field_projection.minimum, field_projection.maximum)
        return [
            {
                "tag": "input",
                "name": name,
                "required": field_projection.required,
                "label": {
                    "tag": "plain_text",
                    "content": _required_label(label, field_projection.required),
                },
                "placeholder": _placeholder("请输入数字"),
                "input_type": "text",
                "max_length": 64,
            }
        ]
    if kind == "date":
        _reject_options(field_projection.options)
        return [
            _plain_div(_required_label(label, field_projection.required)),
            {
                "tag": "date_picker",
                "name": name,
                "required": field_projection.required,
                "placeholder": _placeholder("请选择日期"),
            },
        ]
    if kind in {"boolean", "select"}:
        options = _render_options(field_projection.options)
        return [
            _plain_div(_required_label(label, field_projection.required)),
            {
                "tag": "select_static",
                "name": name,
                "required": field_projection.required,
                "placeholder": _placeholder("请选择"),
                "options": options,
            },
        ]
    if kind == "multi_select":
        options = _render_options(field_projection.options)
        minimum, maximum = _selection_bounds(field_projection, len(options))
        if minimum > 1 or maximum != len(options):
            raise FeishuInteractionRenderError(
                "multi-select bounds are unsupported by native forms",
            )
        return [
            _plain_div(_required_label(label, field_projection.required)),
            {
                "tag": "multi_select_static",
                "name": name,
                "required": field_projection.required,
                "placeholder": _placeholder("请选择"),
                "options": options,
            },
        ]
    raise FeishuInteractionRenderError("field kind is unsupported by native forms")


def _render_options(
    options: Sequence[FeishuFormOptionProjection],
) -> list[dict[str, object]]:
    projected = tuple(options)
    if not 1 <= len(projected) <= _MAX_OPTIONS:
        raise FeishuInteractionRenderError("form options are invalid")
    rendered: list[dict[str, object]] = []
    seen: set[str] = set()
    for option in projected:
        if not isinstance(option, FeishuFormOptionProjection):
            raise FeishuInteractionRenderError("form option projection is invalid")
        value = _required_text(
            option.value,
            max_chars=_MAX_OPTION_VALUE_CHARS,
            field="option value",
        )
        label = _required_text(
            option.label,
            max_chars=_MAX_LABEL_CHARS,
            field="option label",
        )
        if value in seen:
            raise FeishuInteractionRenderError("form option values must be unique")
        seen.add(value)
        rendered.append(
            {
                "text": {"tag": "plain_text", "content": label},
                "value": value,
            }
        )
    return rendered


def _reject_options(options: Sequence[FeishuFormOptionProjection]) -> None:
    if tuple(options):
        raise FeishuInteractionRenderError("field options are unsupported")


def _text_bounds(field_projection: FeishuFormFieldProjection) -> tuple[int, int]:
    minimum = field_projection.min_length
    maximum = field_projection.max_length
    if minimum is None:
        minimum = 0
    if maximum is None:
        maximum = _MAX_PROJECTED_TEXT_CHARS
    if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum <= _MAX_PROJECTED_TEXT_CHARS:
        raise FeishuInteractionRenderError("text bounds are invalid")
    return minimum, maximum


def _selection_bounds(
    field_projection: FeishuFormFieldProjection,
    option_count: int,
) -> tuple[int, int]:
    minimum = field_projection.min_length
    maximum = field_projection.max_length
    if minimum is None:
        minimum = 0
    if maximum is None:
        maximum = option_count
    if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum <= option_count:
        raise FeishuInteractionRenderError("selection bounds are invalid")
    return minimum, maximum


def _validate_number_bounds(minimum: object, maximum: object) -> None:
    for value in (minimum, maximum):
        if value is not None and (not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(float(value))):
            raise FeishuInteractionRenderError("number bounds are invalid")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise FeishuInteractionRenderError("number bounds are invalid")


def _required_text(value: object, *, max_chars: int, field: str) -> str:
    if not _is_bounded_text(value, max_chars):
        raise FeishuInteractionRenderError(f"{field} is invalid")
    return value


def _is_bounded_text(value: object, max_chars: int) -> TypeGuard[str]:
    return type(value) is str and 0 < len(value) <= max_chars and value == value.strip() and all(ord(character) >= 0x20 for character in value)


def _plain_div(content: str) -> dict[str, object]:
    return {
        "tag": "div",
        "text": {"tag": "plain_text", "content": content},
    }


def _required_label(label: str, required: bool) -> str:
    return f"{label} *" if required else label


def _placeholder(content: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": content}


__all__ = [
    "FeishuFormFieldKind",
    "FeishuFormFieldProjection",
    "FeishuFormOptionProjection",
    "FeishuFormProjection",
    "FeishuInteractionCardTransport",
    "FeishuInteractionRenderError",
    "FeishuInteractionRenderer",
    "FeishuTerminalProjection",
    "RenderedFeishuInteractionForm",
    "render_interaction_form",
    "render_interaction_terminal",
]
