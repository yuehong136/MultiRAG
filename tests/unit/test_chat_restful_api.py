import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession

from common.constants import StatusEnum

_CHAT_API_PATH = Path(__file__).resolve().parents[2] / "api/apps/restful_apis/chat_api.py"
_LEGACY_CONVERSATION_APP_PATH = Path(__file__).resolve().parents[2] / "api/apps/conversation_app.py"
_SPEC = importlib.util.spec_from_file_location("chat_api_under_test", _CHAT_API_PATH)
chat_api = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(chat_api)


class Obj(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


def _install_common_fakes(monkeypatch):
    monkeypatch.setattr(chat_api, "get_uuid", lambda: "chat-new")
    monkeypatch.setattr(chat_api.TenantService, "get_by_id", lambda _db, _tenant_id: Obj(llm_id="chat-model"))
    monkeypatch.setattr(chat_api.TenantLLMService, "get_api_key", lambda *_args, **_kwargs: Obj(id=7))
    monkeypatch.setattr(chat_api.KnowledgebaseService, "accessible", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        chat_api.KnowledgebaseService,
        "query",
        lambda *_args, **_kwargs: [Obj(id="kb-1", chunk_num=1, embd_id="embed-model", tenant_embd_id=None)],
    )
    monkeypatch.setattr(
        chat_api.KnowledgebaseService,
        "get_by_id",
        lambda _db, kb_id: Obj(id=kb_id, name="Dataset One", status=StatusEnum.VALID.value),
    )
    monkeypatch.setattr(chat_api, "ensure_tenant_model_id_for_params", lambda _db, _tenant_id, req: req)


def test_prepare_create_payload_accepts_restful_shape(monkeypatch):
    _install_common_fakes(monkeypatch)
    monkeypatch.setattr(chat_api.DialogService, "query", lambda *_args, **_kwargs: [])

    ok, payload = chat_api._prepare_create_payload(
        object(),
        "tenant-1",
        {
            "id": "client-supplied",
            "created_by": "client-supplied",
            "create_time": 123,
            "name": "  Support Bot  ",
            "icon": "avatar-data",
            "dataset_ids": ["kb-1"],
            "llm_id": "chat-model",
            "llm_setting": {"temperature": 0.2},
            "prompt_config": {
                "system": "Answer from {knowledge}",
                "parameters": [{"key": "knowledge", "optional": False}],
                "prologue": "Hi",
                "quote": False,
            },
            "vector_similarity_weight": 0.4,
        },
    )

    assert ok is True
    assert payload["id"] == "chat-new"
    assert payload["tenant_id"] == "tenant-1"
    assert payload["name"] == "Support Bot"
    assert payload["icon"] == "avatar-data"
    assert payload["kb_ids"] == ["kb-1"]
    assert payload["llm_id"] == "chat-model"
    assert payload["llm_setting"] == {"temperature": 0.2}
    assert payload["prompt_config"]["system"] == "Answer from {knowledge}"
    assert payload["prompt_config"]["parameters"] == [{"key": "knowledge", "optional": False}]
    assert payload["prompt_config"]["prologue"] == "Hi"
    assert payload["prompt_config"]["quote"] is False
    assert payload["vector_similarity_weight"] == 0.4
    assert "created_by" not in payload
    assert "create_time" not in payload


def test_prepare_create_payload_rejects_legacy_sdk_shape(monkeypatch):
    _install_common_fakes(monkeypatch)

    ok, message = chat_api._prepare_create_payload(
        object(),
        "tenant-1",
        {
            "name": "Support Bot",
            "avatar": "avatar-data",
            "llm": {"model_name": "chat-model"},
            "prompt": {"prompt": "Answer from {knowledge}"},
        },
    )

    assert ok is False
    assert "Unsupported legacy chat payload fields" in message
    assert "avatar" in message
    assert "llm" in message
    assert "prompt" in message


def test_prepare_create_payload_treats_blank_name_as_required(monkeypatch):
    _install_common_fakes(monkeypatch)

    ok, message = chat_api._prepare_create_payload(
        object(),
        "tenant-1",
        {
            "name": "   ",
            "dataset_ids": [],
        },
    )

    assert ok is False
    assert message == "`name` is required."


def test_prepare_create_payload_allows_prompt_parameter_not_in_system(monkeypatch):
    _install_common_fakes(monkeypatch)
    monkeypatch.setattr(chat_api.DialogService, "query", lambda *_args, **_kwargs: [])

    ok, payload = chat_api._prepare_create_payload(
        object(),
        "tenant-1",
        {
            "name": "Support Bot",
            "dataset_ids": [],
            "prompt_config": {
                "system": "No placeholder here",
                "parameters": [{"key": "knowledge", "optional": False}],
            },
        },
    )

    assert ok is True
    assert payload["prompt_config"]["system"] == "No placeholder here"
    assert payload["prompt_config"]["parameters"] == [{"key": "knowledge", "optional": False}]


def test_patch_payload_merges_nested_settings(monkeypatch):
    _install_common_fakes(monkeypatch)
    current = Obj(
        id="chat-1",
        name="Old Bot",
        kb_ids=["kb-1"],
        prompt_config={
            "system": "Answer from {knowledge}",
            "parameters": [{"key": "knowledge", "optional": False}],
            "prologue": "Old",
        },
        llm_setting={"temperature": 0.1, "top_p": 0.3},
    )
    monkeypatch.setattr(chat_api.DialogService, "get_by_id", lambda _db, _chat_id: current)
    monkeypatch.setattr(chat_api.DialogService, "query", lambda *_args, **_kwargs: [])

    ok, payload = chat_api._prepare_update_payload(
        object(),
        "tenant-1",
        "chat-1",
        {
            "prompt_config": {"prologue": "New"},
            "llm_setting": {"temperature": 0.6},
        },
        merge_nested=True,
    )

    assert ok is True
    assert payload["prompt_config"] == {
        "system": "Answer from {knowledge}",
        "parameters": [{"key": "knowledge", "optional": False}],
        "prologue": "New",
    }
    assert payload["llm_setting"] == {"temperature": 0.6, "top_p": 0.3}


def test_update_payload_allows_knowledge_placeholder_without_sources(monkeypatch):
    _install_common_fakes(monkeypatch)
    current = Obj(
        id="chat-1",
        name="Old Bot",
        kb_ids=[],
        prompt_config={"system": "Old system", "parameters": []},
        llm_setting={},
    )
    monkeypatch.setattr(chat_api.DialogService, "get_by_id", lambda _db, _chat_id: current)
    monkeypatch.setattr(chat_api.DialogService, "query", lambda *_args, **_kwargs: [])

    ok, payload = chat_api._prepare_update_payload(
        object(),
        "tenant-1",
        "chat-1",
        {
            "prompt_config": {
                "system": "Answer with {knowledge}",
                "parameters": [{"key": "knowledge", "optional": False}],
            },
        },
        merge_nested=False,
    )

    assert ok is True
    assert payload["prompt_config"]["system"] == "Answer with {knowledge}"


def test_list_chats_returns_restful_shape(monkeypatch):
    _install_common_fakes(monkeypatch)

    def fake_get_by_tenant_ids(_db, joined, user_id, page, page_size, orderby, desc, keywords, *, id=None, name=None):
        assert joined == []
        assert user_id == "tenant-1"
        assert page == 0
        assert page_size == 0
        assert orderby == "create_time"
        assert desc is True
        assert keywords == ""
        assert id == "chat-1"
        assert name is None
        return [
            {
                "id": "chat-1",
                "tenant_id": "tenant-1",
                "name": "Support Bot",
                "kb_ids": ["kb-1"],
            }
        ], 1

    monkeypatch.setattr(chat_api.DialogService, "get_by_tenant_ids", fake_get_by_tenant_ids)

    response = chat_api.list_chats(
        id="chat-1",
        name=None,
        keywords="",
        page=0,
        page_size=0,
        orderby="create_time",
        desc=True,
        owner_ids=None,
        db=object(),
        tenant_id="tenant-1",
    )
    body = response.body.decode()

    assert '"chats"' in body
    assert '"total":1' in body
    assert '"dataset_ids":["kb-1"]' in body
    assert '"kb_names":["Dataset One"]' in body


def test_build_session_response_renames_chat_fields():
    result = chat_api.build_session_response(
        {
            "id": "session-1",
            "dialog_id": "chat-1",
            "message": [{"role": "assistant", "content": "hello"}],
            "_sa_instance_state": object(),
        }
    )

    assert result["chat_id"] == "chat-1"
    assert result["messages"] == [{"role": "assistant", "content": "hello"}]
    assert "dialog_id" not in result
    assert "message" not in result
    assert "_sa_instance_state" not in result


def test_list_sessions_uses_restful_shape_and_all_rows(monkeypatch):
    captured = {}
    monkeypatch.setattr(chat_api, "_owned_chat_exists", lambda *_args, **_kwargs: True)

    def fake_get_list(_db, chat_id, page, page_size, orderby, desc, session_id, name, user_id):
        captured.update(
            {
                "chat_id": chat_id,
                "page": page,
                "page_size": page_size,
                "orderby": orderby,
                "desc": desc,
                "session_id": session_id,
                "name": name,
                "user_id": user_id,
            }
        )
        return [
            {
                "id": "session-1",
                "dialog_id": "chat-1",
                "message": [{"role": "assistant", "content": "hello"}],
                "reference": [],
            }
        ]

    monkeypatch.setattr(chat_api.ConversationService, "get_list", fake_get_list)

    response = chat_api.list_sessions(
        "chat-1",
        id="session-1",
        name="Demo",
        page=1,
        page_size=0,
        orderby="create_time",
        desc=True,
        user_id="user-1",
        db=object(),
        tenant_id="tenant-1",
    )
    body = json.loads(response.body)

    assert captured == {
        "chat_id": "chat-1",
        "page": 1,
        "page_size": 0,
        "orderby": "create_time",
        "desc": True,
        "session_id": "session-1",
        "name": "Demo",
        "user_id": "user-1",
    }
    assert body["data"][0]["chat_id"] == "chat-1"
    assert body["data"][0]["messages"][0]["content"] == "hello"


def test_update_session_rejects_message_and_reference_changes(monkeypatch):
    monkeypatch.setattr(chat_api, "_owned_chat_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(chat_api.ConversationService, "query", lambda *_args, **_kwargs: [Obj(id="session-1")])

    response = chat_api.update_session(
        "chat-1",
        "session-1",
        chat_api.UpdateSessionRequest.model_validate({"messages": []}),
        db=object(),
        tenant_id="tenant-1",
    )
    assert json.loads(response.body)["message"] == "`messages` cannot be changed."

    response = chat_api.update_session(
        "chat-1",
        "session-1",
        chat_api.UpdateSessionRequest.model_validate({"reference": []}),
        db=object(),
        tenant_id="tenant-1",
    )
    assert json.loads(response.body)["message"] == "`reference` cannot be changed."


def test_session_completion_non_stream_uses_session_rest_path(monkeypatch):
    monkeypatch.setattr(chat_api, "_owned_chat_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(chat_api, "get_uuid", lambda: "msg-1")
    conv = Obj(id="session-1", dialog_id="chat-1", message=[], reference=[])
    dialog = Obj(
        id="chat-1",
        tenant_id="tenant-1",
        llm_id="chat-model",
        tenant_llm_id=1,
        llm_setting={},
        kb_ids=[],
        prompt_config={},
    )
    updates = []
    monkeypatch.setattr(chat_api.ConversationService, "get_by_id", lambda _db, _session_id: conv)
    monkeypatch.setattr(chat_api.DialogService, "get_by_id", lambda _db, _chat_id: dialog)
    monkeypatch.setattr(
        chat_api.ConversationService,
        "update_by_id",
        lambda _db, _session_id, payload: updates.append(payload) or 1,
    )

    db_stub = AsyncSession()

    async def fake_async_chat(dialog_arg, messages_arg, db_arg, stream=True, **kwargs):
        assert dialog_arg is dialog
        assert db_arg is db_stub
        assert stream is False
        assert messages_arg == [{"role": "user", "content": "hi", "id": "msg-1"}]
        assert kwargs == {}
        yield {"answer": "ok", "reference": {}, "audio_binary": None, "final": True}

    monkeypatch.setattr(chat_api, "async_chat", fake_async_chat)

    class RequestStub:
        @staticmethod
        def model_dump(exclude_unset=True):
            return {"messages": [{"role": "user", "content": "hi"}], "stream": False}

    response = asyncio.run(
        chat_api.session_completion(
            "chat-1",
            "session-1",
            RequestStub(),
            db=db_stub,
            tenant_id="tenant-1",
        )
    )
    body = json.loads(response.body)

    assert body["data"]["answer"] == "ok"
    assert body["data"]["session_id"] == "session-1"
    assert body["data"]["id"] == "msg-1"
    assert updates
    assert updates[0]["message"][-1]["content"] == "ok"


def test_related_questions_uses_search_chat_config(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        chat_api.SearchService,
        "get_detail",
        lambda _db, search_id: {
            "search_config": {
                "chat_id": f"{search_id}-model",
                "llm_setting": {"temperature": 0.4, "parameter": {"ignored": True}},
            }
        },
    )

    def fake_get_model_config(db, tenant_id, model_type, model_name):
        captured["model_lookup"] = (db, tenant_id, model_type, model_name)
        return {"id": 7}

    monkeypatch.setattr(chat_api, "get_model_config_by_type_and_name", fake_get_model_config)
    monkeypatch.setattr(chat_api, "get_tenant_default_model_by_type", lambda *_args: {"id": "default"})
    monkeypatch.setattr(chat_api, "load_prompt", lambda name: f"prompt:{name}")

    class FakeLLMBundle:
        def __init__(self, db, tenant_id, config):
            captured["bundle"] = (db, tenant_id, config)

        async def async_chat(self, prompt, messages, gen_conf):
            captured["chat"] = (prompt, messages, gen_conf)
            return "1. first term\n2. second term\nnot numbered"

    monkeypatch.setattr(chat_api, "LLMBundle", FakeLLMBundle)

    response = asyncio.run(
        chat_api.related_questions(
            chat_api.RelatedQuestionsRequest(question="hybrid search", search_id="search-1"),
            db="db",
            tenant_id="tenant-1",
        )
    )
    body = json.loads(response.body)

    assert captured["model_lookup"] == ("db", "tenant-1", "chat", "search-1-model")
    assert captured["bundle"] == ("db", "tenant-1", {"id": 7})
    assert captured["chat"][0] == "prompt:related_question"
    assert captured["chat"][1][0]["content"] == "\nKeywords: hybrid search\nRelated search terms:\n    "
    assert captured["chat"][2] == {"temperature": 0.4}
    assert body["data"] == ["first term", "second term"]


def test_chat_restful_routes_cover_ragflow_b7daf628_static_endpoints():
    routes = {(route.path, method) for route in chat_api.router.routes for method in getattr(route, "methods", set())}

    assert ("/chats/tts", "POST") in routes
    assert ("/chats/transcriptions", "POST") in routes
    assert ("/chats/mindmap", "POST") in routes
    assert ("/chats/related_questions", "POST") in routes
    assert ("/chats/ask", "POST") in routes


def test_legacy_conversation_app_remains_deprecated_for_compatibility():
    assert _LEGACY_CONVERSATION_APP_PATH.exists()
    source = _LEGACY_CONVERSATION_APP_PATH.read_text(encoding="utf-8")
    assert "/v1/conversation/*" in source
    assert "deprecated=True" in source
