"""Real chatbot authorization, model resolution, SDK requests and SQL readback."""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Session

from api.db.db_models import API4Conversation, APIToken, Dialog, Knowledgebase, Search, Tenant, TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider
from api.db.services import conversation_service, tenant_llm_service
from tests.support.agent_update_release import release_api as release_api
from tests.support.agent_update_release import sse_events
from tests.support.resource_journal import ResourceJournal


@pytest.fixture
def chatbot_http(release_api: dict[str, Any], bootstrapped_async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = release_api
    journal = ResourceJournal("chatbot-model-defaults")
    provider, instance, dialog_id, search_id = [uuid4().hex for _ in range(4)]
    model_id = 10**10 + uuid4().int % 10**10
    kb_ids = [uuid4().hex for _ in range(2)]
    beta = uuid4().hex
    wire: list[dict[str, Any]] = []
    journal.event("reserve-owned-rows", database=env["engine"].url.database, provider=provider, instance=instance, model=model_id, dialog=dialog_id, datasets=kb_ids, search=search_id)
    journal.event("reserve-beta-value", token_sha256=hashlib.sha256(beta.encode()).hexdigest(), owner=env["owners"][0])
    with Session(env["engine"]) as writer:
        writer.add(TenantModelProvider(id=provider, tenant_id=env["owners"][0], provider_name="MiniMax", status="active"))
        writer.flush()
        writer.add(TenantModelInstance(id=instance, provider_id=provider, instance_name="controlled", api_key="fixture-key", base_url="http://fixture.invalid/v1", region="", status="active"))
        writer.add(
            TenantLLM(
                id=model_id,
                tenant_id=env["owners"][0],
                llm_factory="MiniMax",
                llm_name="MiniMax-M2",
                mdl_type="chat",
                api_key="fixture-key",
                api_base="http://fixture.invalid/v1",
                status="1",
                used_tokens=0,
            )
        )
        writer.flush()
        writer.add(TenantModel(id=model_id, instance_id=instance, model_name="MiniMax-M2", model_types=["chat"], is_tools=False))
        writer.add(
            Dialog(
                id=dialog_id,
                tenant_id=env["owners"][0],
                name="Default model",
                llm_id="",
                tenant_llm_id=None,
                kb_ids=[],
                rerank_id="",
                prompt_config={"system": "Answer the question", "prologue": "Hello", "parameters": [], "quote": False},
            )
        )
        for owner, kb_id in zip(env["owners"][:2], kb_ids, strict=True):
            writer.add(Knowledgebase(id=kb_id, tenant_id=owner, created_by=owner, name="Scoped dataset", permission="me", embd_id="", parser_config={}))
        writer.add(Search(id=search_id, tenant_id=env["owners"][0], created_by=env["owners"][0], name="Empty search", search_config={"kb_ids": []}))
        writer.execute(sa.update(APIToken).where(APIToken.token == env["keys"][0]).values(beta=beta))
        writer.commit()

    @contextmanager
    def scratch_session() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    monkeypatch.setattr(tenant_llm_service, "db_connection", scratch_session)
    monkeypatch.setattr(tenant_llm_service, "async_session_factory", async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False))

    async def send(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "fixture.invalid"
        body = json.loads(request.content)
        wire.append(body)
        assert body["model"] == "MiniMax-M2"
        if body.get("stream"):
            chunks = [
                {"id": "controlled", "object": "chat.completion.chunk", "created": 1, "model": "MiniMax-M2", "choices": [{"index": 0, "delta": {"content": "<think>reason"}, "finish_reason": None}]},
                {
                    "id": "controlled",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "MiniMax-M2",
                    "choices": [{"index": 0, "delta": {"content": "</think>answer"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                },
            ]
            content = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"}, request=request)
        return httpx.Response(
            200,
            json={
                "id": "controlled",
                "object": "chat.completion",
                "created": 1,
                "model": "MiniMax-M2",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
            request=request,
        )

    transport = httpx.MockTransport(send)
    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", lambda self, url: transport)
    try:
        yield {
            **env,
            "dialog_id": dialog_id,
            "model_id": model_id,
            "provider_id": provider,
            "instance_id": instance,
            "headers": {"Authorization": "Bearer " + beta},
            "wire": wire,
            "kb_ids": kb_ids,
            "journal": journal,
            "search_id": search_id,
        }
    finally:
        with Session(env["engine"]) as writer:
            writer.execute(sa.delete(API4Conversation).where(API4Conversation.dialog_id == dialog_id))
            writer.execute(sa.delete(Dialog).where(Dialog.id == dialog_id))
            writer.execute(sa.delete(Knowledgebase).where(Knowledgebase.id.in_(kb_ids)))
            writer.execute(sa.delete(Search).where(Search.id == search_id))
            writer.execute(sa.update(Tenant).where(Tenant.id == env["owners"][0]).values(llm_id="", tenant_llm_id=None))
            writer.execute(sa.delete(TenantModel).where(TenantModel.id == model_id))
            writer.execute(sa.delete(TenantLLM).where(TenantLLM.id == model_id))
            writer.execute(sa.delete(TenantModelInstance).where(TenantModelInstance.id == instance))
            writer.execute(sa.delete(TenantModelProvider).where(TenantModelProvider.id == provider))
            writer.commit()
        with Session(env["engine"]) as reader:
            absent = [
                reader.get(model, identifier) is None
                for model, identifier in [
                    (Dialog, dialog_id),
                    (Search, search_id),
                    (TenantModel, model_id),
                    (TenantLLM, model_id),
                    (TenantModelInstance, instance),
                    (TenantModelProvider, provider),
                    *((Knowledgebase, identifier) for identifier in kb_ids),
                ]
            ]
            assert all(absent)
            assert not list(reader.scalars(sa.select(API4Conversation.id).where(API4Conversation.dialog_id == dialog_id)))
            journal.event("sql-cleanup-independent-readback", database=env["engine"].url.database, absent=absent, conversations_absent=True)


def start_session(env: dict[str, Any]) -> str:
    response = env["client"].post(f"{env['base']}/api/v1/chatbots/{env['dialog_id']}/completions", headers=env["headers"], json={"stream": True}, timeout=30)
    assert response.status_code == 200, response.text
    events = sse_events(response.text)
    assert events[0]["code"] == 0 and events[0]["data"]["answer"] == "Hello", response.text
    assert not env["wire"]
    identifier = events[0]["data"]["session_id"]
    with Session(env["engine"]) as reader:
        conversation = reader.get(API4Conversation, identifier)
        assert conversation is not None and conversation.dialog_id == env["dialog_id"] and len(conversation.message) == 1
    return identifier


@pytest.mark.parametrize("selection", ["default_id", "default_name", "binding", "binding_stale_name", "numeric_name", "qualified_name"])
@pytest.mark.parametrize("stream", [False, True])
def test_chatbot_http_resolves_server_model_and_commits_answer(chatbot_http: dict[str, Any], selection: str, stream: bool) -> None:
    env = chatbot_http
    with Session(env["engine"]) as writer:
        dialog = writer.get(Dialog, env["dialog_id"])
        tenant = writer.get(Tenant, env["owners"][0])
        assert dialog is not None and tenant is not None
        if selection == "default_id":
            tenant.tenant_llm_id = env["model_id"]
        elif selection == "default_name":
            tenant.llm_id = "MiniMax-M2@MiniMax"
        elif selection.startswith("binding"):
            dialog.tenant_llm_id = env["model_id"]
            dialog.llm_id = "obsolete@provider" if selection == "binding_stale_name" else ""
        else:
            dialog.llm_id = str(env["model_id"]) if selection == "numeric_name" else "MiniMax-M2@MiniMax"
        writer.commit()
        binding = (dialog.llm_id, dialog.tenant_llm_id)
    identifier = start_session(env)
    response = env["client"].post(
        f"{env['base']}/api/v1/chatbots/{env['dialog_id']}/completions", headers=env["headers"], json={"session_id": identifier, "question": "Question", "stream": stream}, timeout=30
    )
    assert response.status_code == 200, response.text
    if stream:
        events = sse_events(response.text)
        assert all(event["code"] == 0 for event in events), response.text
        finals = [event["data"] for event in events if isinstance(event.get("data"), dict) and event["data"].get("final")]
        assert len(finals) == 1 and finals[0]["answer"].endswith("answer"), response.text
        assert any(event.get("data", {}).get("end_to_think") for event in events if isinstance(event.get("data"), dict))
    else:
        assert response.json()["code"] == 0 and response.json()["data"]["answer"] == "answer", response.text
    assert len(env["wire"]) == 1
    with Session(env["engine"]) as reader:
        saved = reader.get(API4Conversation, identifier)
        dialog = reader.get(Dialog, env["dialog_id"])
        model = reader.get(TenantLLM, env["model_id"])
        assert saved is not None and [message["role"] for message in saved.message] == ["assistant", "user", "assistant"]
        assert saved.message[-1]["content"].endswith("answer") and saved.reference[-1]["chunks"] == []
        assert dialog is not None and (dialog.llm_id, dialog.tenant_llm_id) == binding
        assert model is not None and model.used_tokens > 0


@pytest.mark.parametrize("failure", ["foreign", "disabled_provider", "disabled_instance", "disabled_model", "deleted", "no_default"])
def test_chatbot_unavailable_models_fail_without_provider_or_message_commit(chatbot_http: dict[str, Any], failure: str) -> None:
    env = chatbot_http
    identifier = start_session(env)
    with Session(env["engine"]) as writer:
        if failure != "no_default":
            writer.execute(sa.update(Dialog).where(Dialog.id == env["dialog_id"]).values(tenant_llm_id=env["model_id"]))
        if failure == "foreign":
            writer.execute(sa.update(TenantLLM).where(TenantLLM.id == env["model_id"]).values(tenant_id=env["owners"][1]))
            writer.execute(sa.update(TenantModelProvider).where(TenantModelProvider.id == env["provider_id"]).values(tenant_id=env["owners"][1]))
        elif failure == "disabled_provider":
            writer.execute(sa.update(TenantModelProvider).where(TenantModelProvider.id == env["provider_id"]).values(status="inactive"))
        elif failure == "disabled_instance":
            writer.execute(sa.update(TenantModelInstance).where(TenantModelInstance.id == env["instance_id"]).values(status="inactive"))
        elif failure == "disabled_model":
            writer.execute(sa.update(TenantLLM).where(TenantLLM.id == env["model_id"]).values(status="0"))
        elif failure == "deleted":
            writer.execute(sa.delete(TenantModel).where(TenantModel.id == env["model_id"]))
            writer.execute(sa.delete(TenantLLM).where(TenantLLM.id == env["model_id"]))
        writer.commit()
    response = env["client"].post(
        f"{env['base']}/api/v1/chatbots/{env['dialog_id']}/completions", headers=env["headers"], json={"session_id": identifier, "question": "Question", "stream": True}, timeout=30
    )
    assert response.status_code == 200 and sse_events(response.text)[0]["code"] == 500, response.text
    assert not env["wire"]
    with Session(env["engine"]) as reader:
        saved = reader.get(API4Conversation, identifier)
        assert saved is not None and len(saved.message) == 1 and not saved.reference


@pytest.mark.parametrize("selection", ["empty", "saved_empty", "deleted_during_read", "stale", "foreign", "mixed"])
def test_searchbot_empty_or_denied_kbs_never_reach_a_model(chatbot_http: dict[str, Any], selection: str, monkeypatch: pytest.MonkeyPatch) -> None:
    env = chatbot_http
    ids = {"empty": [], "saved_empty": env["kb_ids"][:1], "deleted_during_read": env["kb_ids"][:1], "stale": [uuid4().hex], "foreign": [env["kb_ids"][1]], "mixed": env["kb_ids"]}[selection]
    if selection == "deleted_during_read":
        from api.db.services.knowledgebase_service import KnowledgebaseService

        original = KnowledgebaseService.get_by_ids

        def delete_then_load(cls: Any, db: Session, identifiers: list[str], columns: list[str] | None = None) -> Any:
            env["journal"].event("inject-dataset-delete-after-authorization", database=env["engine"].url.database, id=ids[0])
            with Session(env["engine"]) as writer:
                writer.execute(sa.delete(Knowledgebase).where(Knowledgebase.id == ids[0]))
                writer.commit()
            return original(db, identifiers, columns)

        monkeypatch.setattr(KnowledgebaseService, "get_by_ids", classmethod(delete_then_load))
    payload = {"question": "Question", "kb_ids": ids}
    if selection == "saved_empty":
        payload["search_id"] = env["search_id"]
    response = env["client"].post(f"{env['base']}/api/v1/searchbots/ask", headers=env["headers"], json=payload, timeout=30)
    assert response.status_code == 200, response.text
    if selection in {"empty", "saved_empty", "deleted_during_read"}:
        message = "The selected KB is not valid" if selection == "deleted_during_read" else "No KB selected"
        assert sse_events(response.text)[0]["data"] == {"answer": "**ERROR**: " + message, "reference": {}, "final": True}
    else:
        assert response.json()["code"] == 109, response.text
    assert not env["wire"]
    with Session(env["engine"]) as reader:
        assert reader.get(TenantLLM, env["model_id"]).used_tokens == 0
        assert reader.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.dialog_id == env["dialog_id"])) == 0


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_sync_iframe_snapshot_commits_complete_turn_or_preserves_prior_json(chatbot_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stream: bool, failure: bool) -> None:
    env = chatbot_http
    identifier = start_session(env)
    with Session(env["engine"]) as db:

        def generate(dialog: Dialog, messages: list[dict[str, Any]], session: Session, streaming: bool, **kwargs: Any) -> Iterator[dict[str, Any]]:
            assert messages[-1]["content"] == "Question"
            # This SELECT forces the same autoflush as model resolution. The
            # turn must still exist only in the detached runtime snapshot.
            count = session.scalar(sa.select(sa.func.jsonb_array_length(API4Conversation.message)).where(API4Conversation.id == identifier))
            assert count == 1
            if failure:
                raise RuntimeError("controlled generation failure")
            yield {"answer": "answer", "reference": {}, "final": True}

        monkeypatch.setattr(conversation_service, "chat", generate)
        answers = conversation_service.iframe_completion(db, env["dialog_id"], "Question", identifier, stream=stream, tenant_id=env["owners"][0])
        if failure and not stream:
            with pytest.raises(RuntimeError, match="controlled generation failure"):
                list(answers)
        else:
            frames = list(answers)
            assert frames
            if failure:
                assert json.loads(frames[0].removeprefix("data:"))["code"] == 500
    with Session(env["engine"]) as reader:
        saved = reader.get(API4Conversation, identifier)
        assert saved is not None
        assert [message["role"] for message in saved.message] == (["assistant"] if failure else ["assistant", "user", "assistant"])
        assert saved.message[-1]["content"] == ("Hello" if failure else "answer")
        assert not env["wire"]
