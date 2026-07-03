import json

import pytest

from agent.a2ui import (
    A2UI_CATALOG_ID,
    build_a2ui_event,
    parse_a2ui_commands,
    validate_client_a2ui_messages,
)


def make_commands():
    return [
        {
            "version": "v0.9",
            "createSurface": {
                "surfaceId": "message-card",
                "catalogId": A2UI_CATALOG_ID,
                "sendDataModel": True,
            },
        },
        {
            "version": "v0.9",
            "updateDataModel": {
                "surfaceId": "message-card",
                "path": "/form/name",
                "value": "Alice",
            },
        },
        {
            "version": "v0.9",
            "updateComponents": {
                "surfaceId": "message-card",
                "components": [
                    {
                        "id": "root",
                        "component": "Card",
                        "child": "body",
                    },
                    {
                        "id": "body",
                        "component": "Column",
                        "children": ["title", "name", "submit"],
                    },
                    {
                        "id": "title",
                        "component": "Text",
                        "text": "确认提交？",
                        "variant": "body",
                    },
                    {
                        "id": "name",
                        "component": "TextField",
                        "label": "姓名",
                        "value": {"path": "/form/name"},
                    },
                    {
                        "id": "submit-label",
                        "component": "Text",
                        "text": "提交",
                    },
                    {
                        "id": "submit",
                        "component": "Button",
                        "child": "submit-label",
                        "variant": "primary",
                        "action": {
                            "event": {
                                "name": "submit",
                                "context": {"name": {"path": "/form/name"}},
                            },
                        },
                    },
                ],
            },
        },
    ]


def test_build_a2ui_event_accepts_canonical_v09_basic_catalog_commands():
    event = build_a2ui_event(make_commands())

    assert event["surface_id"] == "message-card"
    assert event["surface_ids"] == ["message-card"]
    assert len(event["commands"]) == 3
    assert event["commands"][0]["createSurface"]["catalogId"] == A2UI_CATALOG_ID


def test_parse_a2ui_commands_accepts_raw_json_array():
    commands = parse_a2ui_commands(json.dumps(make_commands(), ensure_ascii=False))

    assert commands[0]["createSurface"]["surfaceId"] == "message-card"


def test_parse_a2ui_commands_accepts_raw_jsonl_commands():
    commands = parse_a2ui_commands("\n".join(json.dumps(command) for command in make_commands()))

    assert len(commands) == 3
    assert commands[2]["updateComponents"]["surfaceId"] == "message-card"


def test_parse_a2ui_commands_rejects_fenced_markdown():
    with pytest.raises(ValueError, match="not fenced markdown"):
        parse_a2ui_commands("```a2ui\n[]\n```")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda commands: commands[0].update({"version": "v0.8"}),
            "version must be v0.9",
        ),
        (
            lambda commands: commands[0]["createSurface"].update({"catalogId": "local://multirag-agent-card/v1.json"}),
            "catalogId is not supported",
        ),
        (
            lambda commands: commands[0].update({"type": "createSurface"}),
            "version and exactly one command type",
        ),
        (
            lambda commands: commands[0].update({"surface_id": "message-card"}),
            "version and exactly one command type",
        ),
        (
            lambda commands: commands[0].update({"command": "createSurface"}),
            "does not use a 'command' field",
        ),
        (
            lambda commands: commands[2]["updateComponents"]["components"][2].update({"props": {"text": "Nope"}}),
            "schema validation failed",
        ),
        (
            lambda commands: commands[2]["updateComponents"]["components"][5].update({"actions": []}),
            "schema validation failed",
        ),
        (
            lambda commands: commands[2]["updateComponents"]["components"].append({"id": "agree", "component": "CheckBox", "label": "同意", "checked": True}),
            "schema validation failed",
        ),
        (
            lambda commands: commands[2]["updateComponents"]["components"].append({"id": "confirm", "component": "CheckBox", "value": {"path": "/form/confirmed"}}),
            "CheckBox component 'confirm' must include a label",
        ),
        (
            lambda commands: commands[2]["updateComponents"]["components"][0].update({"child": "missing"}),
            "unknown component ids",
        ),
        (
            lambda commands: commands[1]["updateDataModel"].update({"path": "form/name"}),
            "JSON Pointer path",
        ),
    ],
)
def test_build_a2ui_event_rejects_non_canonical_payloads(mutate, message):
    commands = make_commands()
    mutate(commands)

    with pytest.raises(ValueError, match=message):
        build_a2ui_event(commands)


@pytest.mark.parametrize(
    ("component_name", "message"),
    [
        ("Form", "Use Column or Row"),
        ("TextInput", "Use TextField"),
        ("Select", "Use ChoicePicker"),
        ("MultiSelect", "multipleSelection"),
    ],
)
def test_build_a2ui_event_rejects_unsupported_basic_component_names_with_hints(component_name, message):
    commands = make_commands()
    commands[2]["updateComponents"]["components"].append({"id": "invalid", "component": component_name})

    with pytest.raises(ValueError, match=message):
        build_a2ui_event(commands)


def test_build_a2ui_event_rejects_inline_children_before_schema_noise():
    commands = make_commands()
    commands[2]["updateComponents"]["components"][1]["children"] = [{"id": "inline", "component": "Text", "text": "Nope"}]

    with pytest.raises(ValueError, match="children must reference component ids"):
        build_a2ui_event(commands)


def test_build_a2ui_event_requires_create_surface_before_updates():
    commands = make_commands()[1:]

    with pytest.raises(ValueError, match="created before updates"):
        build_a2ui_event(commands)


def test_build_a2ui_event_accepts_multiple_update_components_for_same_surface():
    commands = make_commands()
    commands.append(
        {
            "version": "v0.9",
            "updateComponents": {
                "surfaceId": "message-card",
                "components": [
                    {"id": "root", "component": "Card", "child": "body"},
                    {"id": "body", "component": "Text", "text": "Updated"},
                ],
            },
        }
    )

    assert build_a2ui_event(commands)["surface_ids"] == ["message-card"]


def test_validate_client_a2ui_messages_accepts_standard_action():
    messages = validate_client_a2ui_messages(
        [
            {
                "version": "v0.9",
                "action": {
                    "name": "submit",
                    "surfaceId": "message-card",
                    "sourceComponentId": "submit",
                    "timestamp": "2026-04-29T09:30:00Z",
                    "context": {"grade": ["grade-1"]},
                },
            }
        ]
    )

    assert messages[0]["action"]["sourceComponentId"] == "submit"


def test_validate_client_a2ui_messages_rejects_private_action_input_wrapper():
    with pytest.raises(ValueError, match="must be a list"):
        validate_client_a2ui_messages(
            {
                "__a2ui_action__": {
                    "version": "v0.9",
                    "action": {"name": "submit", "surfaceId": "message-card", "context": {}},
                }
            }
        )
