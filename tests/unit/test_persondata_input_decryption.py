import asyncio
import json

from agent.canvas import Canvas
from agent.persondata_input import PERSONDATA_REDACTED_VALUE
from common.datav_persondata_crypto import decrypt_persondata_prompt

AES_KEY_HEX = "9962707aa9bfa32590233bca0443dfa9173b47ab8e366caabdb27c39c99ceff7"
AES_IV_HEX = "0f4abefe47a7a4953d7f93063ff8e514"
PLAINTEXT = "这是一段person_data prompt内容"
ENCRYPTED = "7sWZbML1Rg8FRDJoPJZdDun7nf7AXWRwYbTWWdJjxEG5tQuqEY5GHjac7qmczYFw"


def _make_begin_only_dsl() -> str:
    return json.dumps(
        {
            "components": {
                "begin": {
                    "obj": {"component_name": "Begin", "params": {}},
                    "downstream": [],
                    "upstream": [],
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


async def _collect_events(canvas: Canvas, inputs: dict):
    return [event async for event in canvas.run(query="run", inputs=inputs)]


def test_decrypt_persondata_prompt_matches_java_aes_cbc_pkcs5():
    assert decrypt_persondata_prompt(ENCRYPTED, AES_KEY_HEX, AES_IV_HEX) == PLAINTEXT


def test_begin_decrypts_only_persondata_and_redacts_events_and_dsl(monkeypatch):
    monkeypatch.setenv("DATAV_PERSONDATA_AES_KEY_HEX", AES_KEY_HEX)
    monkeypatch.setenv("DATAV_PERSONDATA_AES_IV_HEX", AES_IV_HEX)

    canvas = Canvas(_make_begin_only_dsl())
    inputs = {
        "person": {"type": "person_data", "value": ENCRYPTED},
        "plain": {"type": "text", "value": "普通文本"},
    }

    events = asyncio.run(_collect_events(canvas, inputs))

    begin = canvas.get_component_obj("begin")
    assert begin.output("person") == PLAINTEXT
    assert begin.output("plain") == "普通文本"

    serialized_events = json.dumps(events, ensure_ascii=False)
    serialized_canvas = str(canvas)
    assert PLAINTEXT not in serialized_events
    assert PLAINTEXT not in serialized_canvas
    assert PERSONDATA_REDACTED_VALUE in serialized_events
    assert PERSONDATA_REDACTED_VALUE in serialized_canvas


def test_persondata_decrypt_failure_stops_workflow_with_error(monkeypatch):
    monkeypatch.setenv("DATAV_PERSONDATA_AES_KEY_HEX", AES_KEY_HEX)
    monkeypatch.setenv("DATAV_PERSONDATA_AES_IV_HEX", AES_IV_HEX)

    canvas = Canvas(_make_begin_only_dsl())
    inputs = {"person": {"type": "persondata", "value": "not-base64"}}

    events = asyncio.run(_collect_events(canvas, inputs))
    begin_finished = next(event for event in events if event["event"] == "node_finished")

    assert "person_data" in begin_finished["data"]["error"]
    assert not any(event["event"] == "workflow_finished" for event in events)


def test_persondata_dataobject_array_keeps_existing_web_payload_shape(monkeypatch):
    monkeypatch.delenv("DATAV_PERSONDATA_AES_KEY_HEX", raising=False)
    monkeypatch.delenv("DATAV_PERSONDATA_AES_IV_HEX", raising=False)

    canvas = Canvas(_make_begin_only_dsl())
    selected_dataobjects = ["t_jzg_gzjl", "t_jzg_jtcy"]
    inputs = {"person": {"type": "persondata", "value": selected_dataobjects}}

    events = asyncio.run(_collect_events(canvas, inputs))
    begin = canvas.get_component_obj("begin")

    assert begin.output("person") == selected_dataobjects
    assert any(event["event"] == "workflow_finished" for event in events)
