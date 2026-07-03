import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

_CHAT_API_PATH = Path(__file__).resolve().parents[2] / "api/apps/restful_apis/chat_api.py"
_SPEC = importlib.util.spec_from_file_location("chat_feedback_api_under_test", _CHAT_API_PATH)
chat_api = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(chat_api)


class Obj(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


def test_feedback_endpoint_rejects_non_boolean_thumbup(monkeypatch):
    monkeypatch.setattr(chat_api, "_owned_chat_exists", lambda *_args: True)
    monkeypatch.setattr(
        chat_api.ConversationService,
        "get_by_id",
        lambda *_args: Obj(id="session-1", dialog_id="chat-1", message=[], reference=[]),
    )
    update = []
    monkeypatch.setattr(chat_api.ConversationService, "update_by_id", lambda *_args: update.append(_args))

    response = chat_api.update_message_feedback(
        "chat-1",
        "session-1",
        "msg-1",
        Obj(model_dump=lambda **_kwargs: {"thumbup": "yes"}),
        db="db",
        tenant_id="tenant-1",
    )

    body = json.loads(response.body)
    assert "thumbup must be a boolean" in json.dumps(body)
    assert update == []


def test_feedback_endpoint_applies_first_feedback_and_updates_session(monkeypatch):
    monkeypatch.setattr(chat_api, "_owned_chat_exists", lambda *_args: True)
    session = Obj(
        id="session-1",
        dialog_id="chat-1",
        message=[
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "q", "id": "msg-1"},
            {"role": "assistant", "content": "a", "id": "msg-1"},
        ],
        reference=[{"chunks": [{"id": "chunk-1", "dataset_id": "kb-1"}]}],
    )
    monkeypatch.setattr(chat_api.ConversationService, "get_by_id", lambda *_args: session)
    updates = []
    monkeypatch.setattr(chat_api.ConversationService, "update_by_id", lambda *_args: updates.append(_args))
    calls = []
    monkeypatch.setattr(
        chat_api.ChunkFeedbackService,
        "apply_feedback",
        lambda **kwargs: calls.append(kwargs) or {"success_count": 1, "fail_count": 0},
    )

    response = chat_api.update_message_feedback(
        "chat-1",
        "session-1",
        "msg-1",
        Obj(model_dump=lambda **_kwargs: {"thumbup": True}),
        db="db",
        tenant_id="tenant-1",
    )

    body = json.loads(response.body)
    assert body["data"]["messages"][2]["thumbup"] is True
    assert calls[0]["tenant_id"] == "tenant-1"
    assert calls[0]["is_positive"] is True
    assert updates[0][2]["message"][2]["thumbup"] is True


def test_feedback_helper_applies_first_feedback_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        chat_api.ChunkFeedbackService,
        "apply_feedback",
        lambda **kwargs: calls.append(kwargs) or {"success_count": 1, "fail_count": 0},
    )
    payload = {
        "message": [
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "q", "id": "msg-1"},
            {"role": "assistant", "content": "a", "id": "msg-1"},
        ],
        "reference": [{"chunks": [{"id": "chunk-1", "dataset_id": "kb-1"}]}],
    }

    assert chat_api.apply_feedback_to_session_payload("tenant-1", payload, "msg-1", True)

    assert payload["message"][2]["thumbup"] is True
    assert calls == [
        {
            "tenant_id": "tenant-1",
            "reference": payload["reference"][0],
            "is_positive": True,
        }
    ]


def test_feedback_helper_repeated_same_thumb_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(
        chat_api.ChunkFeedbackService,
        "apply_feedback",
        lambda **kwargs: calls.append(kwargs) or {"success_count": 1, "fail_count": 0},
    )
    payload = {
        "message": [
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "q", "id": "msg-1"},
            {"role": "assistant", "content": "a", "id": "msg-1", "thumbup": True},
        ],
        "reference": [{"chunks": [{"id": "chunk-1", "dataset_id": "kb-1"}]}],
    }

    assert chat_api.apply_feedback_to_session_payload("tenant-1", payload, "msg-1", True)

    assert calls == []


def test_feedback_helper_thumb_flip_applies_undo_then_new(monkeypatch):
    calls = []
    monkeypatch.setattr(
        chat_api.ChunkFeedbackService,
        "apply_feedback",
        lambda **kwargs: calls.append(kwargs) or {"success_count": 1, "fail_count": 0},
    )
    payload = {
        "message": [
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "q", "id": "msg-1"},
            {"role": "assistant", "content": "a", "id": "msg-1", "thumbup": True},
        ],
        "reference": [{"chunks": [{"id": "chunk-1", "dataset_id": "kb-1"}]}],
    }

    assert chat_api.apply_feedback_to_session_payload("tenant-1", payload, "msg-1", False, "bad answer")

    assert payload["message"][2]["thumbup"] is False
    assert payload["message"][2]["feedback"] == "bad answer"
    assert [call["is_positive"] for call in calls] == [False, False]
