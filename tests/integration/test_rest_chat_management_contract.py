"""Registered REST management routes with authenticated SQL roundtrips."""

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Conversation, Dialog, File, UserTenant
from common.constants import RetCode
from tests.support.rest_chat_contract import RestChatContract
from tests.support.rest_chat_contract import rest_chat_contract as rest_chat_contract
from tests.support.rest_chat_contract import runtime_upload_api as runtime_upload_api
from tests.support.runtime_upload import read_object


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


@pytest.mark.parametrize("credential", ["jwt", "api_key"])
def test_session_concurrent_crud_and_hundred_delete(rest_chat_contract: RestChatContract, credential: str) -> None:
    ctx = rest_chat_contract
    chat = ctx.create_chat(credential=credential)
    path = f"/chats/{chat['id']}/sessions"

    def create(index: int) -> dict[str, Any]:
        response = ctx.request("POST", path, credential=credential, json={"name": f"session-{index:03d}"})
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        return response.json()["data"]

    with ThreadPoolExecutor(max_workers=5) as pool:
        sessions = list(pool.map(create, range(20)))
    ids = [item["id"] for item in sessions]
    assert len(set(ids)) == 20
    assert all(ctx.read_session(identifier)["dialog_id"] == chat["id"] for identifier in ids)

    def update(identifier: str) -> None:
        name = f"updated-{identifier}"
        for _ in range(2):
            response = ctx.request("PATCH", f"{path}/{identifier}", credential=credential, json={"name": name})
            assert response.status_code == 200 and response.json()["code"] == 0, response.text
        assert ctx.read_session(identifier)["name"] == name

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(update, ids))

    def read(_: int) -> set[str]:
        response = ctx.request("GET", path, credential=credential, params={"page_size": 100})
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        return {item["id"] for item in response.json()["data"]}

    with ThreadPoolExecutor(max_workers=5) as pool:
        assert all(actual == set(ids) for actual in pool.map(read, range(10)))

    def delete(identifier: str) -> None:
        response = ctx.request("DELETE", path, credential=credential, json={"ids": [identifier]})
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        assert response.json()["data"] is True
        assert ctx.read_session(identifier) is None

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(delete, ids))
        bulk = list(pool.map(create, range(100)))
    bulk_ids = [item["id"] for item in bulk]
    assert len(set(bulk_ids)) == 100
    assert all(ctx.read_session(identifier) is not None for identifier in bulk_ids)
    response = ctx.request("DELETE", path, credential=credential, json={"ids": bulk_ids})
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    assert response.json()["data"] is True
    assert all(ctx.read_session(identifier) is None for identifier in bulk_ids)
    assert read(0) == set()


def test_session_filters_stable_pages_and_strict_query(rest_chat_contract: RestChatContract) -> None:
    ctx = rest_chat_contract
    chat = ctx.create_chat()
    path = f"/chats/{chat['id']}/sessions"
    sessions = []
    for name in ["same"] * 5 + ["other", "final"]:
        response = ctx.request("POST", path, json={"name": name})
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        sessions.append(response.json()["data"])
    # Force tied sort values; stable paging must still include each ID once.
    ids = [item["id"] for item in sessions]
    ctx.runtime["journal"].event("reserve-session-sort-fixture", ids=ids, table="conversation")
    with Session(ctx.runtime["engine"]) as db:
        db.execute(sa.update(Conversation).where(Conversation.id.in_(ids)).values(create_time=1234567890000))
        db.commit()
    before = ctx.snapshot()
    for descending in (False, True):
        expected = sorted(ids, reverse=descending)
        actual = []
        for page in range(1, 6):
            response = ctx.request("GET", path, params={"page": page, "page_size": 2, "orderby": "create_time", "desc": descending})
            assert response.status_code == 200 and response.json()["code"] == 0, response.text
            actual.extend(item["id"] for item in response.json()["data"])
        assert actual == expected
    for params, expected in (
        ({"name": "same"}, ids[:5]),
        ({"id": ids[0]}, ids[:1]),
        ({"id": ids[0], "name": "other"}, []),
        ({"id": "missing"}, []),
        ({"user_id": ctx.runtime["owners"][1]}, []),
        ({"page": 0, "page_size": 0}, ids),
    ):
        response = ctx.request("GET", path, params=params)
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        assert {item["id"] for item in response.json()["data"]} == set(expected)
    invalid = [
        {"page": "-1"},
        {"page": "1.5"},
        {"page": "1.0"},
        {"page": "NaN"},
        {"page_size": "-1"},
        {"page_size": "101"},
        {"page_size": "1.0"},
        {"desc": "unknown"},
        [("page", "1"), ("page", "2")],
        [("page_size", "1"), ("page_size", "2")],
    ]
    for params in invalid:
        response = ctx.request("GET", path, params=params)
        assert response.status_code == 422, (params, response.text)
        assert ctx.snapshot() == before
    for order in ("name", "update_time"):
        response = ctx.request("GET", path, params={"orderby": order, "desc": False})
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        values = response.json()["data"]
        assert [(item[order], item["id"]) for item in values] == sorted((item[order], item["id"]) for item in values)
    assert ctx.snapshot() == before


def test_session_auth_validation_and_deleted_chat_preserve_sql(rest_chat_contract: RestChatContract) -> None:
    ctx = rest_chat_contract
    chat = ctx.create_chat()
    path = f"/chats/{chat['id']}/sessions"
    created = ctx.request("POST", path, json={"name": "kept"}).json()
    assert created["code"] == 0
    identifier = created["data"]["id"]
    before = ctx.snapshot()
    for credential in (None, "invalid_token"):
        ctx.runtime["invalid_token"] = "invalid-token"
        for method, target, body in (("POST", path, {}), ("GET", path, None), ("PATCH", f"{path}/{identifier}", {"name": "denied"}), ("DELETE", path, {"ids": [identifier]})):
            response = ctx.request(method, target, credential=credential, json=body)
            assert response.status_code == 401, response.text
            assert ctx.snapshot() == before
    for name in ("", " ", 1):
        for method, target in (("POST", path), ("PATCH", f"{path}/{identifier}")):
            response = ctx.request(method, target, json={"name": name})
            if isinstance(name, str):
                assert response.status_code == 200 and response.json()["code"] == RetCode.DATA_ERROR, response.text
            else:
                assert response.status_code == 422, response.text
            assert ctx.snapshot() == before
    assert ctx.request("DELETE", "/chats", json={"ids": [chat["id"]]}).json()["code"] == 0
    before = ctx.snapshot()
    for method, target, body in (("POST", path, {}), ("GET", path, None), ("PATCH", f"{path}/{identifier}", {"name": "denied"}), ("DELETE", path, {"delete_all": True})):
        response = ctx.request(method, target, json=body)
        assert response.status_code == 200 and response.json()["code"] == RetCode.AUTHENTICATION_ERROR, response.text
        assert ctx.snapshot() == before


@pytest.mark.parametrize("credential", ["jwt", "api_key"])
def test_file_upload_persists_sql_and_object_bytes(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, credential: str) -> None:
    import requests

    from api.apps.services import file_api_service

    env = runtime_upload_api
    owner = env["owners"][credential == "api_key"]
    folder, file_id = uuid4().hex, uuid4().hex
    name, blob = "uploaded.txt", b"uploaded bytes \xe4\xb8\xad\xe6\x96\x87"
    env["journal"].event("reserve-file-upload", database=env["engine"].url.database, owner=owner, folder=folder, file_id=file_id, bucket=env["bucket"], object_key=f"{folder}/{name}")

    @contextmanager
    def scratch_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    monkeypatch.setattr(file_api_service, "db_connection", scratch_db)
    monkeypatch.setattr(file_api_service, "get_uuid", lambda: file_id)
    try:
        with Session(env["engine"]) as db:
            db.add(File(id=folder, parent_id=folder, tenant_id=owner, created_by=owner, name="upload-folder", type="folder", location="", size=0))
            db.commit()
        response = requests.post(
            f"{env['base']}/api/v1/files", headers={"Authorization": f"Bearer {env[credential]}"}, data={"parent_id": folder}, files={"file": (name, blob, "text/plain")}, timeout=30
        )
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        data = response.json()["data"]
        assert len(data) == 1 and data[0]["id"] == file_id
        with Session(env["engine"]) as db:
            row = db.get(File, file_id)
            assert row is not None
            assert (row.name, row.parent_id, row.tenant_id, row.created_by, row.size, row.location) == (name, folder, owner, owner, len(blob), name)
        bucket, key = env["storage_adapter"]._resolve_bucket_and_path(folder, name)
        assert bucket == env["bucket"]
        assert read_object(env["storage"], bucket, key) == blob
        listing = requests.get(f"{env['base']}/api/v1/files", headers={"Authorization": f"Bearer {env[credential]}"}, params={"parent_id": folder}, timeout=30)
        assert listing.status_code == 200 and listing.json()["code"] == 0, listing.text
        assert [item["id"] for item in listing.json()["data"]["files"]] == [file_id]
        env["journal"].event("upload-readback", file_id=file_id, sql_committed=True, bytes_equal=True, listed=True)
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(File).where(File.id.in_([file_id, folder])))
            db.commit()
        with env["engine"].connect() as connection:
            remaining = connection.scalar(sa.select(sa.func.count()).select_from(File).where(File.id.in_([file_id, folder])))
        env["journal"].event("upload-sql-cleanup-readback", ids=[file_id, folder], remaining=remaining)
        assert remaining == 0
