import asyncio
import json

from agent.a2ui import A2UI_CATALOG_ID
from agent.canvas import Canvas


def make_commands(surface_id="card-1"):
    return [
        {
            "version": "v0.9",
            "createSurface": {
                "surfaceId": surface_id,
                "catalogId": A2UI_CATALOG_ID,
            },
        },
        {
            "version": "v0.9",
            "updateComponents": {
                "surfaceId": surface_id,
                "components": [
                    {"id": "root", "component": "Card", "child": "title"},
                    {"id": "title", "component": "Text", "text": "Ready"},
                ],
            },
        },
    ]


def make_dsl(commands):
    return json.dumps(
        {
            "components": {
                "begin": {
                    "obj": {"component_name": "Begin", "params": {}},
                    "downstream": ["A2UI:card"],
                    "upstream": [],
                },
                "A2UI:card": {
                    "obj": {
                        "component_name": "A2UI",
                        "params": {"commands": commands},
                    },
                    "downstream": [],
                    "upstream": ["begin"],
                },
            },
            "path": [],
            "retrieval": [],
            "history": [],
            "globals": {
                "sys.query": "",
                "sys.user_id": "",
                "sys.conversation_turns": 0,
                "sys.files": [],
            },
        },
        ensure_ascii=False,
    )


async def collect_events(canvas):
    return [event async for event in canvas.run(query="show card")]


def test_a2ui_node_emits_a2ui_command_without_message_event():
    canvas = Canvas(make_dsl([json.dumps(make_commands(), ensure_ascii=False)]))

    events = asyncio.run(collect_events(canvas))

    assert any(event["event"] == "a2ui_command" for event in events)
    assert not any(event["event"] == "message" for event in events)
    assert not any(event["event"] == "message_end" for event in events)
    a2ui_event = next(event for event in events if event["event"] == "a2ui_command")
    assert a2ui_event["data"]["surface_ids"] == ["card-1"]
    assert len(a2ui_event["data"]["commands"]) == 2


def test_a2ui_node_rejects_invalid_commands_as_node_error():
    commands = make_commands()
    commands[0]["version"] = "v0.8"
    canvas = Canvas(make_dsl([json.dumps(commands, ensure_ascii=False)]))

    events = asyncio.run(collect_events(canvas))

    assert not any(event["event"] == "a2ui_command" for event in events)
    node_finished = next(event for event in events if event["event"] == "node_finished" and event["data"]["component_type"] == "A2UI")
    assert "version must be v0.9" in node_finished["data"]["error"]
