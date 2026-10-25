"""Incremental REST document matrices; real HTTP/auth/SQL/MinIO, no model calls.

Existing task/Redis/Milvus lifecycle suites cover successful parsing. These cases
add the missing parameter, auth and bounded concurrency readbacks without
restoring retired document GET/PUT contracts or permissive upstream assertions.
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import sqlalchemy as sa

from api.constants import FILE_NAME_LEN_LIMIT
from api.db.db_models import Document
from common import settings
from core.utils.redis_conn import REDIS_CONN
from tests.support.rest_document_contract import call, snapshot
from tests.support.rest_document_contract import rest_documents as rest_documents
from tests.support.runtime_upload import read_object


def upload(env: dict[str, Any], names: list[str]) -> list[dict[str, Any]]:
    response = call(env, "POST", f"/datasets/{env['datasets'][0]}/documents", files=[("file", (name, ("contents: " + name).encode())) for name in names])
    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0, response.text
    return response.json()["data"]


def documents(env: dict[str, Any]) -> dict[str, dict[str, Any]]:
    with env["engine"].connect() as db:
        return {row["id"]: dict(row) for row in db.execute(sa.select(Document.__table__)).mappings()}


@pytest.mark.parametrize("credential_kind", ["keys", "jwt"])
def test_document_list_parameter_matrix_and_concurrent_readback(rest_documents: dict[str, Any], credential_kind: str) -> None:
    env = rest_documents
    docs = upload(env, [f"matrix_{i}.txt" for i in range(5)])
    path = f"/datasets/{env['datasets'][0]}/documents"
    token = env[credential_kind][0]
    before = snapshot(env)
    # Time filters deliberately preserve the pre-time-filter total in this API.
    filters = [
        ({}, 5, 5),
        ({"create_time_from": 9999999999000}, 0, 5),
        ({"create_time_to": 1}, 0, 5),
        ({"create_time_from": 0, "create_time_to": 9999999999000}, 5, 5),
        ({"keywords": ""}, 5, 5),
        ({"keywords": "0"}, 1, 1),
        ({"keywords": "unknown"}, 0, 0),
        ({"name": docs[0]["name"]}, 1, 1),
        ({"id": docs[0]["id"]}, 1, 1),
        ({"id": docs[0]["id"], "name": docs[1]["name"]}, 0, 0),
        ({"run": "UNSTART"}, 5, 5),
    ]
    for params, length, total in filters:
        response = call(env, "GET", path, credential=token, params=params)
        assert response.status_code == 200
        assert response.json()["code"] == 0, (params, response.text)
        assert response.json()["data"]["total"] == total, params
        assert len(response.json()["data"]["docs"]) == length, params
    for page, expected in [(1, 2), (2, 2), (3, 1), (4, 0)]:
        response = call(env, "GET", path, credential=token, params={"page": page, "page_size": 2})
        assert response.json()["code"] == 0
        assert len(response.json()["data"]["docs"]) == expected and response.json()["data"]["total"] == 5
    for key in ["create_time", "update_time", "name"]:
        for desc in [True, False]:
            response = call(env, "GET", path, credential=token, params={"orderby": key, "desc": str(desc)})
            assert response.status_code == 200 and response.json()["code"] == 0
            values = [row[key] for row in response.json()["data"]["docs"]]
            assert values == sorted(values, reverse=desc)
    for params in [{"page": -1}, {"page": "a"}, {"page_size": -1}, {"page_size": "a"}, {"page_size": 101}, {"desc": "unknown"}]:
        response = call(env, "GET", path, credential=token, params=params)
        assert response.status_code == 422 and response.json()["detail"], (params, response.text)
    for params, message in [
        ({"metadata_condition": "{bad"}, "metadata_condition must be valid JSON."),
        ({"metadata_condition": "[1]"}, "metadata_condition must be an object."),
        ({"run": "BOGUS"}, "Invalid filter run status conditions: BOGUS"),
        ({"id": "missing"}, "You don't own the document missing."),
    ]:
        response = call(env, "GET", path, credential=token, params=params)
        assert response.status_code == 200 and response.json() == {"code": 102, "message": message}
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: call(env, "GET", path, credential=token), range(12)))
    expected_ids = {doc["id"] for doc in docs}
    for response in responses:
        assert response.status_code == 200 and response.json()["code"] == 0
        assert {doc["id"] for doc in response.json()["data"]["docs"]} == expected_ids
    assert snapshot(env) == before
    env["journal"].event("assertions", list_matrix=True, concurrent_reads=12, sql_objects_unchanged=True)


def test_upload_download_duplicate_empty_binary_and_concurrency(rest_documents: dict[str, Any]) -> None:
    env = rest_documents
    path = f"/datasets/{env['datasets'][0]}/documents"
    rows = upload(env, ["duplicate.txt", "duplicate.txt", "data.md", "data.json", "data.eml", "data.html"])
    assert [row["name"] for row in rows[:2]] == ["duplicate.txt", "duplicate(1).txt"]
    assert upload(env, ["duplicate.txt"])[0]["name"] == "duplicate(2).txt"
    empty = call(env, "POST", path, files=[("file", ("empty.txt", b""))])
    assert empty.status_code == 200 and empty.json()["code"] == 0 and empty.json()["data"][0]["size"] == 0
    names = ["a" * (FILE_NAME_LEN_LIMIT - 4) + ".txt", "!#$%&'()+,-.;=@[]^_`{}~.txt"]
    rows += upload(env, names)
    with ThreadPoolExecutor(max_workers=4) as executor:
        batches = list(executor.map(lambda i: upload(env, [f"parallel_{i}.txt"]), range(12)))
    assert len({batch[0]["id"] for batch in batches}) == 12
    stored = documents(env)
    assert len(stored) == 22
    for doc in stored.values():
        assert doc["kb_id"] == env["datasets"][0] and doc["created_by"] == env["owners"][0]
        content = read_object(env["storage"], env["bucket"], f"{doc['kb_id']}/{doc['location']}")
        assert len(content) == doc["size"]
    before = snapshot(env)

    def download(identifier: str) -> None:
        response = call(env, "GET", f"/documents/{identifier}/download")
        doc = stored[identifier]
        original = before["objects"][f"{doc['kb_id']}/{doc['location']}"]
        assert response.status_code == 200 and response.content == original
        assert response.headers["Content-Disposition"].startswith("attachment")

    identifiers = list(stored)
    for identifier in identifiers[:6]:
        download(identifier)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(download, identifiers + identifiers[:5]))
    assert snapshot(env) == before
    env["journal"].event("assertions", uploads=len(stored), download_bytes_exact=True, concurrent_downloads=len(identifiers) + 5)


@pytest.mark.parametrize("credential_kind", ["keys", "jwt"])
def test_auth_and_invalid_selections_preserve_sql_objects_index_queue(rest_documents: dict[str, Any], credential_kind: str) -> None:
    env = rest_documents
    doc = upload(env, ["guard.txt"])[0]
    path = f"/datasets/{env['datasets'][0]}/documents"
    before = snapshot(env)
    cases = [
        ("GET", path, {}),
        ("POST", path, {"files": [("file", ("forbidden.txt", b"forbidden"))]}),
        ("DELETE", path, {"json": {"ids": [doc["id"]]}}),
        ("POST", path + "/parse", {"json": {"document_ids": [doc["id"]]}}),
        ("POST", path + "/stop", {"json": {"document_ids": [doc["id"]]}}),
        ("GET", f"/documents/{doc['id']}/download", {}),
    ]
    for token in [None, "invalid-token"]:
        for method, route, kwargs in cases:
            response = call(env, method, route, credential=token, **kwargs)
            assert response.status_code == 401, (method, route, response.text)
            if route == path:
                assert response.json() == {"retcode": 109, "retmsg": "`Authorization` can't be empty" if token is None else "Authentication error: invalid credentials!", "data": False}
            else:
                assert response.json()["code"] == 401 and response.json()["data"] is None
            assert snapshot(env) == before
    for method, route, kwargs in cases:
        response = call(env, method, route, credential=env[credential_kind][1], **kwargs)
        assert response.status_code == (404 if route.endswith("/download") else 200)
        assert response.json()["code"] in {102, 109}, (method, route, response.text)
        assert snapshot(env) == before
    for action in ["parse", "stop"]:
        for payload in [{"document_ids": []}, {"document_ids": "invalid"}, "not json", {}]:
            response = call(env, "POST", path + "/" + action, credential=env[credential_kind][0], json=payload)
            assert response.status_code == 422 and response.json()["detail"]
        response = call(env, "POST", path + "/" + action, credential=env[credential_kind][0], json={"document_ids": ["missing"]})
        assert response.status_code == 200 and response.json() == {"code": 102, "message": "Documents not found: ['missing']"}
    for payload in [{}, {"ids": []}, {"ids": ["missing", doc["id"]]}, {"ids": [doc["id"], "missing"]}, {"ids": [doc["id"]], "delete_all": True}]:
        response = call(env, "DELETE", path, credential=env[credential_kind][0], json=payload)
        assert response.status_code == 200 and response.json()["code"] == 102
    for payload in ["not json", {"delete_all": "true"}, {"delete_all": 1}]:
        response = call(env, "DELETE", path, credential=env[credential_kind][0], json=payload)
        assert response.status_code == 422 and response.json()["detail"]
    for files, code, message in [
        (None, 101, "No file part!"),
        ([("file", ("bad.exe", b"unsupported"))], 500, "bad.exe: This type of file has not been supported yet!"),
        ([("file", ("a" * FILE_NAME_LEN_LIMIT + ".txt", b"too long"))], 101, f"File name must be {FILE_NAME_LEN_LIMIT} bytes or less."),
    ]:
        response = call(env, "POST", path, credential=env[credential_kind][0], files=files)
        assert response.status_code == 200 and response.json() == {"code": code, "message": message}
        assert snapshot(env) == before
    assert snapshot(env) == before
    assert not REDIS_CONN.REDIS.exists(env["queue"])
    assert all(not settings.docStoreConn.has_collection(name) for name in env["collections"])
    env["journal"].event("assertions", auth_matrix=True, invalid_selection_matrix=True, sql_objects_unchanged=True, queue_absent=True, indexes_absent=True)


def test_delete_deduplication_repetition_bulk_and_concurrent_readback(rest_documents: dict[str, Any]) -> None:
    env = rest_documents
    path = f"/datasets/{env['datasets'][0]}/documents"
    docs = upload(env, [f"delete_{i}.txt" for i in range(20)])
    identifiers = [row["id"] for row in docs]
    first = call(env, "DELETE", path, json={"ids": identifiers[:2] * 2})
    assert first.status_code == 200 and first.json() == {"code": 0, "data": {"deleted": 2}}
    repeated = call(env, "DELETE", path, json={"ids": identifiers[:2]})
    assert repeated.status_code == 200 and repeated.json()["code"] == 102
    assert len(documents(env)) == 18
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda identifier: call(env, "DELETE", path, json={"ids": [identifier]}), identifiers[2:10]))
    for response in responses:
        assert response.status_code == 200 and response.json()["code"] == 0 and response.json()["data"] == {"deleted": 1}
    assert len(documents(env)) == 10
    bulk = call(env, "DELETE", path, json={"ids": identifiers[10:]})
    assert bulk.status_code == 200 and bulk.json()["code"] == 0 and bulk.json()["data"] == {"deleted": 10}
    assert not documents(env)
    assert not list(env["storage"].list_objects(env["bucket"], recursive=True))
    upload(env, ["all.txt", "all2.txt"])
    all_response = call(env, "DELETE", path, json={"delete_all": True})
    assert all_response.status_code == 200 and all_response.json()["code"] == 0 and all_response.json()["data"] == {"deleted": 2}
    assert not documents(env) and not list(env["storage"].list_objects(env["bucket"], recursive=True))
    assert not REDIS_CONN.REDIS.exists(env["queue"])
    env["journal"].event("assertions", deduplicated_deleted=2, concurrent_deleted=8, bulk_deleted=10, delete_all=2, sql_objects_empty=True)
