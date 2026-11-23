"""Owned REST chat resources, real HTTP/SQL, and a controlled provider boundary."""

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import LLM, Conversation, Dialog, Knowledgebase, Tenant, TenantModel, TenantModelInstance, TenantModelProvider
from tests.support.registered_model import register_model
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@dataclass
class RestChatContract:
    runtime: dict[str, Any]
    factory: str
    datasets: dict[str, str]
    provider: dict[str, Any]

    def request(self, method: str, path: str, *, credential: str | None = "jwt", **kwargs: Any) -> requests.Response:
        if method in {"POST", "PATCH", "PUT", "DELETE"}:
            self.runtime["journal"].event("reserve-chat-request", method=method, path=path, owners=self.runtime["owners"], tables=["dialog", "conversation"])
        headers = {"Authorization": f"Bearer {self.runtime[credential]}"} if credential else {}
        return requests.request(method, f"{self.runtime['base']}/api/v1{path}", headers=headers, timeout=30, **kwargs)

    def create_chat(self, *, credential: str = "jwt", **fields: Any) -> dict[str, Any]:
        response = self.request("POST", "/chats", credential=credential, json={"name": uuid4().hex, "dataset_ids": [], **fields})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["code"] == 0, body
        return body["data"]

    def snapshot(self) -> dict[str, Any]:
        with Session(self.runtime["engine"]) as db:
            chats = list(db.scalars(sa.select(Dialog).where(Dialog.tenant_id.in_(self.runtime["owners"])).order_by(Dialog.id)))
            sessions = list(db.scalars(sa.select(Conversation).where(Conversation.dialog_id.in_([row.id for row in chats])).order_by(Conversation.id)))
            return {"chats": [row.to_dict() for row in chats], "sessions": [row.to_dict() for row in sessions]}

    def read_chat(self, chat_id: str) -> dict[str, Any]:
        with Session(self.runtime["engine"]) as db:
            row = db.get(Dialog, chat_id)
            assert row is not None
            return row.to_dict()

    def read_session(self, session_id: str) -> dict[str, Any] | None:
        with Session(self.runtime["engine"]) as db:
            row = db.get(Conversation, session_id)
            return row.to_dict() if row is not None else None


@pytest.fixture
def rest_chat_contract(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[RestChatContract]:
    import core.llm as providers
    from api.db.services import tenant_llm_service
    from common import settings
    from core.prompts import generator

    env = runtime_upload_api
    owners = env["owners"]
    factory = f"RestContract-{uuid4().hex}"
    datasets = {key: uuid4().hex for key in ("parsed", "unparsed", "foreign")}
    state: dict[str, Any] = {"calls": [], "answer": "Controlled answer", "failure": False}
    record = env["record"]["rest_chat"] = {"factory": factory, "datasets": datasets, "owners": owners}
    env["journal"].event(
        "reserve-chat-scope",
        owners=owners,
        factory=factory,
        datasets=datasets,
        tables=["dialog", "conversation", "knowledgebase", "tenant_model", "tenant_model_instance", "tenant_model_provider", "llm"],
    )

    def save_record() -> None:
        env["record_path"].write_text(json.dumps(env["record"]))

    scoped_providers = sa.select(TenantModelProvider.id).where(TenantModelProvider.tenant_id.in_(owners), TenantModelProvider.provider_name == factory)

    def cleanup_rows() -> None:
        with Session(env["engine"]) as db:
            ids = list(db.scalars(sa.select(Dialog.id).where(Dialog.tenant_id.in_(owners))))
            session_ids = list(db.scalars(sa.select(Conversation.id).where(Conversation.dialog_id.in_(ids))))
            record.update(chats=ids, sessions=session_ids)
            save_record()
            db.execute(sa.delete(Conversation).where(Conversation.id.in_(session_ids)))
            db.execute(sa.delete(Dialog).where(Dialog.id.in_(ids)))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id.in_(list(datasets.values()))))
            provider_ids = list(db.scalars(scoped_providers))
            db.execute(sa.delete(TenantModel).where(TenantModel.provider_id.in_(provider_ids)))
            db.execute(sa.delete(TenantModelInstance).where(TenantModelInstance.provider_id.in_(provider_ids)))
            db.execute(sa.delete(TenantModelProvider).where(TenantModelProvider.id.in_(scoped_providers)))
            db.execute(sa.delete(LLM).where(LLM.fid == factory))
            db.commit()
        with env["engine"].connect() as db:
            remaining = {
                model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model).where(condition))
                for model, condition in (
                    (Conversation, Conversation.id.in_(session_ids)),
                    (Dialog, Dialog.id.in_(ids)),
                    (Knowledgebase, Knowledgebase.id.in_(list(datasets.values()))),
                    (TenantModel, TenantModel.provider_id.in_(provider_ids)),
                    (TenantModelInstance, TenantModelInstance.provider_id.in_(provider_ids)),
                    (TenantModelProvider, TenantModelProvider.id.in_(scoped_providers)),
                    (LLM, LLM.fid == factory),
                )
            }
        record["remaining"] = remaining
        env["journal"].event("chat-cleanup-readback", remaining=remaining)
        save_record()
        assert not any(remaining.values()), remaining

    class ControlledChat:
        def __init__(self, key: str, model: str, **kwargs: Any) -> None:
            assert key == "controlled" and model == "chat"

        async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> tuple[str, int]:
            state["calls"].append(deepcopy({"system": system, "history": history, "gen_conf": gen_conf}))
            if state["failure"]:
                raise RuntimeError("Controlled chat failure")
            return state["answer"], 0

        async def async_chat_streamly(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> AsyncIterator[str | int]:
            state["calls"].append(deepcopy({"system": system, "history": history, "gen_conf": gen_conf}))
            if state["failure"]:
                raise RuntimeError("Controlled chat failure")
            yield state["answer"]
            yield 0

    monkeypatch.setitem(providers.ChatModel, factory, ControlledChat)
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [*settings.FACTORY_LLM_INFOS, {"name": factory}])

    @contextmanager
    def scratch_usage_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    monkeypatch.setattr(tenant_llm_service, "db_connection", scratch_usage_db)
    # Multi-turn refinement creates its own legacy bundle. Keep that real SQL
    # resolution on this fixture's scratch database as well.
    monkeypatch.setattr(generator, "db_connection", scratch_usage_db)
    save_record()
    with ExitStack() as cleanup:
        cleanup.callback(cleanup_rows)
        with Session(env["engine"]) as db:
            for kind in ("chat", "rerank", "speech2text", "tts"):
                db.add(LLM(fid=factory, llm_name=kind, mdl_type=kind, tags=kind.upper(), max_tokens=8192, is_tools=False))
                for owner in owners:
                    model = register_model(db, tenant_id=owner, provider_name=factory, model_name=kind, model_types=(kind,), api_key="controlled")
                    tenant = db.get(Tenant, owner)
                    assert tenant is not None
                    field = {"chat": "llm_id", "rerank": "rerank_id", "speech2text": "asr_id", "tts": "tts_id"}[kind]
                    setattr(tenant, field, model.id)
                    setattr(tenant, f"tenant_{field}", model.id)
            for key, kb_id in datasets.items():
                owner = owners[1 if key == "foreign" else 0]
                db.add(Knowledgebase(id=kb_id, tenant_id=owner, created_by=owner, name=key, embd_id="controlled-embedding", chunk_num=0 if key == "unparsed" else 1))
            db.commit()
        yield RestChatContract(env, factory, datasets, state)


def sse_frames(response: requests.Response) -> list[dict[str, Any] | str]:
    return ["[DONE]" if value == "[DONE]" else json.loads(value) for line in response.text.splitlines() if line.startswith("data:") and (value := line[5:].strip())]
