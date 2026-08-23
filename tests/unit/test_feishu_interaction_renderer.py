"""Feishu native-form renderer and whole-card presentation contracts."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

from api.channel_execution.interaction_forms import (
    InteractionFormField,
    InteractionFormOption,
    InteractionFormProjection,
)
from api.channels.feishu.interaction_renderer import (
    FeishuFormProjection,
    FeishuInteractionCardTransport,
    FeishuInteractionRenderer,
    FeishuInteractionRenderError,
    render_interaction_form,
)


class _CardTransport:
    def __init__(self) -> None:
        self.updates: list[tuple[str, str]] = []

    async def update_card(self, message_id: str, card_json: str) -> None:
        self.updates.append((message_id, card_json))


def _option(value: str, label: str) -> InteractionFormOption:
    return InteractionFormOption(value=value, label=label)


def _projection() -> InteractionFormProjection:
    return InteractionFormProjection(
        message="请补充请假信息",
        fields=(
            InteractionFormField(
                name="f_reason_opaque",
                kind="text",
                label="请假原因",
                required=True,
                min_length=1,
                max_length=1000,
            ),
            InteractionFormField(
                name="f_days_opaque",
                kind="number",
                label="请假天数",
                required=True,
                minimum=0.5,
                maximum=30,
            ),
            InteractionFormField(
                name="f_confirmed_opaque",
                kind="boolean",
                label="信息填写准确",
                options=(
                    _option("o_true_opaque", "是"),
                    _option("o_false_opaque", "否"),
                ),
            ),
            InteractionFormField(
                name="f_start_opaque",
                kind="date",
                label="开始日期",
                required=True,
            ),
            InteractionFormField(
                name="f_type_opaque",
                kind="select",
                label="请假类型",
                options=(
                    _option("o_annual_opaque", "年假"),
                    _option("o_sick_opaque", "病假"),
                ),
            ),
            InteractionFormField(
                name="f_labels_opaque",
                kind="multi_select",
                label="标签",
                options=(
                    _option("o_paid_opaque", "带薪"),
                    _option("o_urgent_opaque", "紧急"),
                ),
                min_length=0,
                max_length=2,
            ),
        ),
    )


def _render() -> tuple[dict[str, object], tuple[str, ...], str]:
    rendered = render_interaction_form(
        action_id="opaque-action",
        action_nonce="opaque-nonce",
        revision=7,
        expires_at=datetime(2026, 8, 25, 8, 30, tzinfo=UTC),
        projection=_projection(),
    )
    return json.loads(rendered.card_json), rendered.field_names, rendered.card_json


def test_native_form_consumes_structural_projection_not_raw_input_requests() -> None:
    parameters = inspect.signature(render_interaction_form).parameters

    assert "projection" in parameters
    assert "input_requests" not in parameters
    assert isinstance(_projection(), FeishuFormProjection)


def test_native_form_maps_only_projection_fields_and_opaque_routing() -> None:
    card, field_names, card_json = _render()

    assert card["schema"] == "2.0"
    assert card["config"] == {
        "update_multi": False,
        "summary": {"content": "等待补充信息"},
    }
    form = card["body"]["elements"][0]
    assert form["tag"] == "form"
    assert form["name"] == "interaction_form"
    components = [element for element in form["elements"] if element.get("name", "").startswith("f_")]
    assert [component["tag"] for component in components] == [
        "input",
        "input",
        "select_static",
        "date_picker",
        "select_static",
        "multi_select_static",
    ]
    assert components[0]["input_type"] == "multiline_text"
    assert components[0]["max_length"] == 1000
    assert components[0]["required"] is True
    assert components[1]["placeholder"]["content"] == "请输入数字"
    assert components[2]["options"] == [
        {
            "text": {"tag": "plain_text", "content": "是"},
            "value": "o_true_opaque",
        },
        {
            "text": {"tag": "plain_text", "content": "否"},
            "value": "o_false_opaque",
        },
    ]
    submit = form["elements"][-1]
    assert submit["tag"] == "button"
    assert submit["form_action_type"] == "submit"
    assert submit["behaviors"] == [
        {
            "type": "callback",
            "value": {
                "action_id": "opaque-action",
                "nonce": "opaque-nonce",
                "revision": 7,
                "action": "accept",
            },
        }
    ]
    cancel = card["body"]["elements"][1]
    assert cancel["tag"] == "button"
    assert cancel["behaviors"] == [
        {
            "type": "callback",
            "value": {
                "action_id": "opaque-action",
                "nonce": "opaque-nonce",
                "revision": 7,
                "action": "cancel",
            },
        }
    ]
    assert field_names == (
        "f_reason_opaque",
        "f_days_opaque",
        "f_confirmed_opaque",
        "f_start_opaque",
        "f_type_opaque",
        "f_labels_opaque",
    )
    assert "有效期至 2026-08-25 08:30 UTC" in card_json
    assert "requestState" not in card_json
    assert "original_arguments" not in card_json
    assert "Principal" not in card_json


def test_native_form_rejects_duplicate_field_and_option_names() -> None:
    field = InteractionFormField(
        name="f_duplicate",
        kind="select",
        label="类型",
        options=(
            _option("o_duplicate", "甲"),
            _option("o_duplicate", "乙"),
        ),
    )
    projection = InteractionFormProjection(
        message="请补充信息",
        fields=(field,),
    )
    with pytest.raises(FeishuInteractionRenderError, match="option values"):
        render_interaction_form(
            action_id="opaque-action",
            action_nonce="opaque-nonce",
            revision=1,
            expires_at=datetime(2026, 8, 25, tzinfo=UTC),
            projection=projection,
        )

    duplicate_fields = InteractionFormProjection(
        message="请补充信息",
        fields=(
            InteractionFormField(name="f_duplicate", kind="text", label="甲"),
            InteractionFormField(name="f_duplicate", kind="text", label="乙"),
        ),
    )
    with pytest.raises(FeishuInteractionRenderError, match="field names"):
        render_interaction_form(
            action_id="opaque-action",
            action_nonce="opaque-nonce",
            revision=1,
            expires_at=datetime(2026, 8, 25, tzinfo=UTC),
            projection=duplicate_fields,
        )


def test_native_form_rejects_projection_outside_cardkit_bounds() -> None:
    unsupported_multi = InteractionFormProjection(
        message="请补充信息",
        fields=(
            InteractionFormField(
                name="f_multi",
                kind="multi_select",
                label="至少选两项",
                options=(
                    _option("o_a", "甲"),
                    _option("o_b", "乙"),
                    _option("o_c", "丙"),
                ),
                min_length=2,
                max_length=3,
            ),
        ),
    )
    with pytest.raises(FeishuInteractionRenderError, match="multi-select bounds"):
        render_interaction_form(
            action_id="opaque-action",
            action_nonce="opaque-nonce",
            revision=1,
            expires_at=datetime(2026, 8, 25, tzinfo=UTC),
            projection=unsupported_multi,
        )

    too_many = cast(
        FeishuFormProjection,
        SimpleNamespace(
            message="请补充信息",
            fields=tuple(
                SimpleNamespace(
                    name=f"f_{index}",
                    kind="text",
                    label=f"字段 {index}",
                    required=False,
                    options=(),
                    min_length=0,
                    max_length=100,
                    minimum=None,
                    maximum=None,
                )
                for index in range(17)
            ),
        ),
    )
    with pytest.raises(FeishuInteractionRenderError, match="field count"):
        render_interaction_form(
            action_id="opaque-action",
            action_nonce="opaque-nonce",
            revision=1,
            expires_at=datetime(2026, 8, 25, tzinfo=UTC),
            projection=too_many,
        )


@pytest.mark.parametrize(
    ("action_nonce", "expires_at"),
    [
        ("x" * 256, datetime(2026, 8, 25, tzinfo=UTC)),
        ("opaque-nonce", datetime(2026, 8, 25)),
    ],
)
def test_native_form_rejects_unbounded_nonce_or_naive_expiration(
    action_nonce: str,
    expires_at: datetime,
) -> None:
    with pytest.raises(FeishuInteractionRenderError):
        render_interaction_form(
            action_id="opaque-action",
            action_nonce=action_nonce,
            revision=1,
            expires_at=expires_at,
            projection=_projection(),
        )


@pytest.mark.asyncio
async def test_renderer_updates_the_original_finished_reply_message() -> None:
    transport = _CardTransport()
    renderer = FeishuInteractionRenderer(transport)

    rendered = await renderer.present(
        reply_message_id="om-original-reply",
        action_id="opaque-action",
        action_nonce="opaque-nonce",
        revision=2,
        expires_at=datetime(2026, 8, 25, tzinfo=UTC),
        projection=_projection(),
    )

    assert isinstance(transport, FeishuInteractionCardTransport)
    assert transport.updates == [
        ("om-original-reply", rendered.card_json),
    ]
