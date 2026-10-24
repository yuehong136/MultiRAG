"""Registered REST management routes with authenticated SQL roundtrips."""

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Conversation, Dialog, UserTenant
from common.constants import RetCode
from tests.support.rest_chat_contract import RestChatContract
from tests.support.rest_chat_contract import rest_chat_contract as rest_chat_contract
from tests.support.rest_chat_contract import runtime_upload_api as runtime_upload_api


@pytest.mark.parametrize("credential", ["jwt", "api_key"])
def test_create_patch_replace_and_projection(rest_chat_contract: RestChatContract, credential: str) -> None:
    ctx = rest_chat_contract
    owner = ctx.runtime["owners"][credential == "api_key"]
    chat = ctx.create_chat(credential=credential, name="  管理合同  ", llm_id=None, icon="data:image/png;base64,c2NyYXRjaA==")
    chat_id = chat["id"]
    assert chat["name"] == "管理合同" and chat["tenant_id"] == owner
    assert chat["llm_id"] == f"chat@{ctx.factory}" and chat["llm_setting"] == {}
    assert chat["dataset_ids"] == [] and chat["kb_names"] == []
    assert "{knowledge}" in chat["prompt_config"]["system"]
    assert chat["prompt_config"]["parameters"] == [{"key": "knowledge", "optional": False}]
    assert not {"kb_ids", "avatar", "prompt", "llm"}.intersection(chat)
    row = ctx.read_chat(chat_id)
    assert row["tenant_id"] == owner and row["kb_ids"] == [] and row["icon"] == chat["icon"]
    for field in ("name", "llm_id", "llm_setting", "prompt_config"):
        assert row[field] == chat[field]

    patch = {"prompt_config": {"prologue": "你好", "quote": False}, "llm_setting": {"temperature": 0}, "tenant_id": "forged", "kb_names": ["response only"]}
    response = ctx.request("PATCH", f"/chats/{chat_id}", credential=credential, json=patch)
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    actual = ctx.read_chat(chat_id)
    assert actual["prompt_config"] == {**chat["prompt_config"], "prologue": "你好", "quote": False}
    assert actual["llm_setting"] == {"temperature": 0} and actual["tenant_id"] == owner
    response = ctx.request("PUT", f"/chats/{chat_id}", credential=credential, json={"llm_setting": {"top_p": 0.5}, "prompt_config": {"system": "replacement", "parameters": []}})
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    actual = ctx.read_chat(chat_id)
    assert actual["llm_setting"] == {"top_p": 0.5}
    assert actual["prompt_config"] == {"system": "replacement", "parameters": []}
    read = ctx.request("GET", f"/chats/{chat_id}", credential=credential).json()
    assert read["code"] == 0 and read["data"]["prompt_config"] == actual["prompt_config"]


def test_dataset_and_model_guards_preserve_sql(rest_chat_contract: RestChatContract) -> None:
    ctx = rest_chat_contract
    chat = ctx.create_chat(dataset_ids=[ctx.datasets["parsed"]], rerank_id=f"rerank@{ctx.factory}", llm_id=f"chat@{ctx.factory}")
    assert chat["dataset_ids"] == [ctx.datasets["parsed"]] and chat["kb_names"] == ["parsed"]
    row = ctx.read_chat(chat["id"])
    assert row["tenant_llm_id"] and row["tenant_rerank_id"] and row["rerank_id"] == f"rerank@{ctx.factory}"
    before = ctx.snapshot()
    invalid_fields: list[dict[str, Any]] = [
        {"name": " "},
        {"name": 1},
        {"name": "中" * 86},
        {"dataset_ids": "wrong"},
        {"dataset_ids": [ctx.datasets["foreign"]]},
        {"dataset_ids": [ctx.datasets["unparsed"]]},
        {"dataset_ids": [uuid4().hex]},
        {"llm_id": "missing-model"},
        {"llm_id": f"chat@missing-{ctx.factory}"},
        {"rerank_id": "missing-reranker"},
        {"llm_setting": []},
        {"prompt_config": "wrong"},
        {"prompt": {}},
        {"llm": {}},
        {"avatar": "legacy"},
    ]
    for fields in invalid_fields:
        for method, path in (("POST", "/chats"), ("PATCH", f"/chats/{chat['id']}"), ("PUT", f"/chats/{chat['id']}")):
            response = ctx.request(method, path, json={"name": uuid4().hex, **fields})
            assert response.status_code == 200 and response.json()["code"] == RetCode.DATA_ERROR, (fields, response.text)
            assert ctx.snapshot() == before
    response = ctx.request("POST", "/chats", json={"name": uuid4().hex, "tenant_id": ctx.runtime["owners"][1]})
    assert response.status_code == 200 and response.json()["code"] == RetCode.DATA_ERROR
    assert ctx.snapshot() == before
    longest = ctx.create_chat(name="a" * 255)
    assert ctx.read_chat(longest["id"])["name"] == "a" * 255
    duplicate = ctx.request("POST", "/chats", json={"name": longest["name"]})
    assert duplicate.json()["code"] == RetCode.DATA_ERROR


@pytest.mark.parametrize("credential", ["jwt", "api_key"])
def test_list_pagination_sort_and_authorized_owners(rest_chat_contract: RestChatContract, credential: str) -> None:
    ctx = rest_chat_contract
    index = credential == "api_key"
    owner, other = ctx.runtime["owners"][index], ctx.runtime["owners"][not index]
    own = [ctx.create_chat(credential=credential, name=f"ordered-{i}") for i in range(5)]
    foreign = ctx.create_chat(credential="jwt" if index else "api_key", name="foreign")
    before = ctx.snapshot()
    for page, expected in ((1, own[:2]), (2, own[2:4]), (3, own[4:]), (4, [])):
        response = ctx.request("GET", "/chats", credential=credential, params={"page": page, "page_size": 2, "orderby": "name", "desc": False})
        assert response.status_code == 200 and response.json()["code"] == 0
        data = response.json()["data"]
        assert data["total"] == 5 and [item["id"] for item in data["chats"]] == [item["id"] for item in expected]
    for params, ids in (({"keywords": "ordered-2"}, [own[2]["id"]]), ({"id": own[0]["id"], "keywords": "missing"}, [own[0]["id"]]), ({"name": "ordered-1"}, [own[1]["id"]])):
        data = ctx.request("GET", "/chats", credential=credential, params=params).json()["data"]
        assert [item["id"] for item in data["chats"]] == ids and data["total"] == len(ids)
    for params in ({"page": "invalid"}, {"page_size": "invalid"}, {"desc": "invalid"}):
        assert ctx.request("GET", "/chats", credential=credential, params=params).status_code == 422
    denied = ctx.request("GET", "/chats", credential=credential, params={"owner_ids": other}).json()
    assert denied["code"] == RetCode.OPERATING_ERROR and ctx.snapshot() == before
    membership_id = uuid4().hex
    ctx.runtime["record"]["rest_chat"]["membership"] = membership_id
    ctx.runtime["record_path"].write_text(json.dumps(ctx.runtime["record"]))
    with Session(ctx.runtime["engine"]) as db:
        db.add(UserTenant(id=membership_id, user_id=owner, tenant_id=other, role="normal", invited_by=other))
        db.commit()
    for params in ({}, [("owner_ids", owner), ("owner_ids", other)]):
        data = ctx.request("GET", "/chats", credential=credential, params=params).json()["data"]
        assert data["total"] == 6 and {item["id"] for item in data["chats"]} == {item["id"] for item in [*own, foreign]}
    assert ctx.request("GET", f"/chats/{foreign['id']}", credential=credential).json()["code"] == 0
    for method, path, body in (("PATCH", f"/chats/{foreign['id']}", {"name": "denied"}), ("DELETE", f"/chats/{foreign['id']}", None)):
        assert ctx.request(method, path, credential=credential, json=body).json()["code"] == RetCode.AUTHENTICATION_ERROR
    assert ctx.snapshot() == before


@pytest.mark.parametrize("credential", ["jwt", "api_key"])
def test_bulk_delete_partial_duplicate_and_scope(rest_chat_contract: RestChatContract, credential: str) -> None:
    ctx = rest_chat_contract
    own = [ctx.create_chat(credential=credential) for _ in range(3)]
    foreign = ctx.create_chat(credential="jwt" if credential == "api_key" else "api_key")
    before = ctx.snapshot()
    for body in ({}, {"ids": []}, {"ids": None}):
        assert ctx.request("DELETE", "/chats", credential=credential, json=body).json()["code"] == 0
        assert ctx.snapshot() == before
    assert ctx.request("DELETE", "/chats", credential=credential, json={"ids": "invalid"}).status_code == 422
    result = ctx.request("DELETE", "/chats", credential=credential, json={"ids": [own[0]["id"], own[0]["id"], foreign["id"], "missing"]}).json()
    assert result["code"] == 0 and result["data"]["success_count"] == 1
    assert len(result["data"]["errors"]) == 3
    assert ctx.read_chat(own[0]["id"])["status"] == "0"
    assert all(ctx.read_chat(item["id"])["status"] == "1" for item in [*own[1:], foreign])
    before = ctx.snapshot()
    assert ctx.request("DELETE", "/chats", credential=credential, json={"ids": [own[0]["id"], foreign["id"]]}).json()["code"] == RetCode.DATA_ERROR
    assert ctx.snapshot() == before
    result = ctx.request("DELETE", "/chats", credential=credential, json={"delete_all": True}).json()
    assert result["code"] == 0 and result["data"]["success_count"] == 2
    assert ctx.read_chat(foreign["id"])["status"] == "1"


def test_concurrent_and_hundred_chat_batch_delete(rest_chat_contract: RestChatContract) -> None:
    ctx = rest_chat_contract
    owner = ctx.runtime["owners"][0]
    ids = [uuid4().hex for _ in range(106)]
    ctx.runtime["record"]["rest_chat"]["bulk_chats"] = ids
    ctx.runtime["record_path"].write_text(json.dumps(ctx.runtime["record"]))
    with Session(ctx.runtime["engine"]) as db:
        db.add_all([Dialog(id=chat_id, tenant_id=owner, name=chat_id, llm_id=f"chat@{ctx.factory}") for chat_id in ids])
        db.commit()

    def delete_one(chat_id: str) -> dict[str, Any]:
        return ctx.request("DELETE", "/chats", json={"ids": [chat_id]}).json()

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(delete_one, ids[:6]))
    assert all(result["code"] == 0 and result["data"]["success_count"] == 1 for result in results)
    result = ctx.request("DELETE", "/chats", json={"ids": ids[6:]}).json()
    assert result["code"] == 0 and result["data"]["success_count"] == 100
    with Session(ctx.runtime["engine"]) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(Dialog).where(Dialog.id.in_(ids), Dialog.status == "0")) == 106


@pytest.mark.parametrize("credential", ["jwt", "api_key"])
def test_session_projection_partial_delete_and_denial(rest_chat_contract: RestChatContract, credential: str) -> None:
    ctx = rest_chat_contract
    other = "jwt" if credential == "api_key" else "api_key"
    owner = ctx.runtime["owners"][credential == "api_key"]
    chat, foreign = ctx.create_chat(credential=credential, prompt_config={"prologue": "welcome"}), ctx.create_chat(credential=other)
    sessions = []
    for name in ("first", "second"):
        body = ctx.request("POST", f"/chats/{chat['id']}/sessions", credential=credential, json={"name": f"  {name}  ", "user_id": "forged"}).json()
        assert body["code"] == 0
        session = body["data"]
        assert session["chat_id"] == chat["id"] and session["user_id"] == owner
        assert session["messages"] == [{"role": "assistant", "content": "welcome"}]
        assert not {"dialog_id", "message"}.intersection(session)
        persisted = ctx.read_session(session["id"])
        assert persisted["name"] == name and persisted["user_id"] == owner
        sessions.append(session["id"])
    foreign_session = ctx.request("POST", f"/chats/{foreign['id']}/sessions", credential=other, json={}).json()["data"]["id"]
    listing = ctx.request("GET", f"/chats/{chat['id']}/sessions", credential=credential, params={"page": 1, "page_size": 1, "orderby": "name", "desc": False}).json()
    assert listing["code"] == 0 and [item["id"] for item in listing["data"]] == sessions[:1]
    before = ctx.snapshot()
    for method, path, body in (
        ("POST", f"/chats/{foreign['id']}/sessions", {}),
        ("PATCH", f"/chats/{chat['id']}/sessions/{foreign_session}", {"name": "denied"}),
        ("DELETE", f"/chats/{chat['id']}/sessions", {"ids": [foreign_session]}),
    ):
        assert ctx.request(method, path, credential=credential, json=body).json()["code"] != 0
        assert ctx.snapshot() == before
    result = ctx.request("DELETE", f"/chats/{chat['id']}/sessions", credential=credential, json={"ids": [sessions[0], sessions[0], foreign_session, "missing"]}).json()
    assert result["code"] == 0 and result["data"]["success_count"] == 1 and len(result["data"]["errors"]) == 3
    assert ctx.read_session(sessions[0]) is None and ctx.read_session(foreign_session) is not None
    assert ctx.request("DELETE", f"/chats/{chat['id']}/sessions", credential=credential, json={"delete_all": True}).json()["code"] == 0
    with Session(ctx.runtime["engine"]) as db:
        assert not list(db.scalars(sa.select(Conversation).where(Conversation.dialog_id == chat["id"])))


def test_unauthenticated_management_has_no_effect(rest_chat_contract: RestChatContract) -> None:
    ctx = rest_chat_contract
    chat = ctx.create_chat()
    before = ctx.snapshot()
    for method, path, body in (("POST", "/chats", {"name": "denied"}), ("GET", "/chats", None), ("PATCH", f"/chats/{chat['id']}", {"name": "denied"}), ("DELETE", "/chats", {"delete_all": True})):
        assert ctx.request(method, path, credential=None, json=body).status_code == 401
    assert ctx.snapshot() == before
