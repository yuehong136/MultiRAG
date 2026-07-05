import asyncio
import json

from agent.canvas import Canvas
from agent.persondata_input import PERSONDATA_REDACTED_VALUE
from common.datav_persondata_crypto import decrypt_persondata_prompt

# 测试专用假密钥（顺序字节模式，显然非生产值）；向量由 common.datav_persondata_crypto 同算法生成
AES_KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"  # gitleaks:allow
AES_IV_HEX = "000102030405060708090a0b0c0d0e0f"  # gitleaks:allow
PLAINTEXT = "这是一段person_data prompt内容"
ENCRYPTED = "ZR1lF653jI/tbVaTMqvD3WQ+WKpuQ1S+z6aKuOXGBABkCDLYpHxnfbxCQPFzS4jS"


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
