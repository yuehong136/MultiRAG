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


def upload(env: dict[str, Any], names: list[str], *, timeout: float = 30) -> list[dict[str, Any]]:
    response = call(env, "POST", f"/datasets/{env['datasets'][0]}/documents", files=[("file", (name, ("contents: " + name).encode())) for name in names], timeout=timeout)
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
        expected_code = 109 if method == "POST" and route == path else 102
        assert response.json()["code"] == expected_code, (method, route, response.text)
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
    missing = call(env, "GET", "/documents/missing/download", credential=env[credential_kind][0])
    assert missing.status_code == 404 and missing.json() == {"code": 102, "message": "Document not found!", "data": None}
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


def test_dataset_and_document_metadata_config_roundtrip_and_denials(rest_documents: dict[str, Any]) -> None:
    env = rest_documents
    doc = upload(env, ["metadata_config.txt"])[0]
    dataset = env["datasets"][0]
    dataset_path = f"/datasets/{dataset}/metadata/config"
    doc_path = f"/datasets/{dataset}/documents/{doc['id']}/metadata/config"
    original = call(env, "GET", dataset_path)
    assert original.status_code == 200 and original.json()["code"] == 0
    assert original.json()["data"] == {"metadata": [], "built_in_metadata": [], "enabled": False}
    configuration = {
        "metadata": [{"key": "author", "type": "string", "description": "Author"}, {"key": "tags", "type": "list", "enum": ["one", "two"]}],
        "built_in_metadata": [{"key": "size", "type": "number"}],
        "enabled": True,
    }
    updated = call(env, "PUT", dataset_path, json=configuration)
    assert updated.status_code == 200 and updated.json()["code"] == 0
    assert updated.json()["data"] == configuration
    assert call(env, "GET", dataset_path).json()["data"] == configuration
    with env["engine"].connect() as db:
        from api.db.db_models import Knowledgebase

        stored = db.scalar(sa.select(Knowledgebase.parser_config).where(Knowledgebase.id == dataset))
    assert stored["metadata"] == configuration["metadata"] and stored["enable_metadata"] is True
    # An omitted field is a partial update, and must not silently clear templates.
    assert call(env, "PUT", dataset_path, json={}).json()["data"] == configuration
    template = {"metadata": {"author": "alice", "tags": ["one", "two"]}}
    response = call(env, "PUT", doc_path, json=template)
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    assert documents(env)[doc["id"]]["parser_config"]["metadata"] == template["metadata"]
    before = snapshot(env)
    for credential in (None, "invalid-token", env["keys"][1]):
        for method, path, payload in [("GET", dataset_path, None), ("PUT", dataset_path, configuration), ("PUT", doc_path, template)]:
            result = call(env, method, path, credential=credential, **({"json": payload} if payload is not None else {}))
            assert result.status_code == (200 if credential == env["keys"][1] else 401)
            assert result.json().get("code", result.json().get("retcode")) != 0
            assert snapshot(env) == before
    for path, payload in [("/datasets/missing/metadata/config", configuration), (f"/datasets/{dataset}/documents/missing/metadata/config", template)]:
        result = call(env, "PUT", path, json=payload)
        assert result.status_code == 200 and result.json()["code"] == 102
        assert snapshot(env) == before
    response = call(env, "PUT", doc_path, json={})
    assert response.status_code == 422 and snapshot(env) == before
    env["journal"].event("assertions", metadata_configuration_http_sql_roundtrip=True, no_forbidden_side_effects=True)


def test_metadata_batch_selection_filter_update_delete_and_readback(rest_documents: dict[str, Any]) -> None:
    from api.db.db_models import DocumentMetadata

    env = rest_documents
    docs = upload(env, ["batch_0.txt", "batch_1.txt", "batch_2.txt"])
    ids = [row["id"] for row in docs]
    path = f"/datasets/{env['datasets'][0]}/documents/metadatas"

    def metadata() -> dict[str, Any]:
        with env["engine"].connect() as db:
            return {row.id: row.meta_fields for row in db.execute(sa.select(DocumentMetadata.id, DocumentMetadata.meta_fields)).all()}

    def update(payload: dict[str, Any], count: int) -> None:
        response = call(env, "PATCH", path, json=payload)
        assert response.status_code == 200 and response.json() == {"code": 0, "data": {"updated": count, "matched_docs": count}}, response.text

    update({"selector": {"document_ids": ids}, "updates": [{"key": "author", "value": "alice"}, {"key": "status", "value": "processed"}]}, 3)
    assert metadata() == {identifier: {"author": "alice", "status": "processed"} for identifier in ids}
    update({"selector": {"document_ids": ids[:1]}, "updates": [{"key": "status", "value": "ready"}]}, 1)
    update(
        {
            "selector": {"metadata_condition": {"logic": "and", "conditions": [{"name": "status", "comparison_operator": "is", "value": "ready"}]}},
            "updates": [{"key": "author", "value": "filtered"}],
            "deletes": [{"key": "status"}],
        },
        1,
    )
    assert metadata()[ids[0]] == {"author": "filtered"}
    assert metadata()[ids[1]] == {"author": "alice", "status": "processed"}
    for selector in [{"document_ids": []}, {"metadata_condition": {"conditions": [{"name": "absent", "comparison_operator": "is", "value": "none"}]}}]:
        before = snapshot(env)
        update({"selector": selector, "updates": [{"key": "author", "value": "forbidden"}]}, 0)
        assert snapshot(env) == before
    before = snapshot(env)
    for payload in [
        {"selector": {"document_ids": [ids[0], "missing"]}, "updates": [{"key": "author", "value": "bad"}]},
        {"selector": {"document_ids": "bad"}},
        *({"selector": {"metadata_condition": value}} for value in [[], False, 0, ""]),
        {"selector": [1]},
        {"updates": {}},
        {"updates": [{"key": ""}]},
        {"deletes": [{"bad": "x"}]},
    ]:
        response = call(env, "PATCH", path, json=payload)
        assert response.status_code == 200 and response.json()["code"] == 102, response.text
        assert snapshot(env) == before
    for credential in (None, "invalid-token", env["keys"][1]):
        response = call(env, "PATCH", path, credential=credential, json={"selector": {"document_ids": ids}, "updates": [{"key": "author", "value": "bad"}]})
        assert response.status_code == (200 if credential == env["keys"][1] else 401)
        assert response.json().get("code", response.json().get("retcode")) != 0
        assert snapshot(env) == before
    update({"selector": {"document_ids": ids}, "deletes": [{"key": "author"}, {"key": "status"}]}, 3)
    assert all(not fields for fields in metadata().values())
    env["journal"].event("assertions", metadata_batch_real_sql=True, explicit_empty_never_broadened=True, no_forbidden_side_effects=True)


def test_bulk_upload_and_delete_over_one_hundred_documents(rest_documents: dict[str, Any]) -> None:
    env = rest_documents
    docs = upload(env, [f"bulk_{index}.txt" for index in range(120)], timeout=120)
    assert len(docs) == len(documents(env)) == 120
    path = f"/datasets/{env['datasets'][0]}/documents"
    pages = [call(env, "GET", path, params={"page": page, "page_size": 100}).json()["data"]["docs"] for page in (1, 2, 3)]
    assert [len(page) for page in pages] == [100, 20, 0]
    assert {row["id"] for page in pages for row in page} == {doc["id"] for doc in docs}
    response = call(env, "DELETE", path, json={"ids": [doc["id"] for doc in docs]}, timeout=120)
    assert response.status_code == 200 and response.json() == {"code": 0, "data": {"deleted": 120}}
    assert not documents(env) and not list(env["storage"].list_objects(env["bucket"], recursive=True))
    env["journal"].event("assertions", bulk_deleted=120, page_sizes=[100, 20, 0], sql_objects_empty=True)


def test_real_mcp_and_cli_consumers_read_all_dataset_pages(rest_documents: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import importlib
    import importlib.util
    from pathlib import Path
    from urllib.parse import urlsplit
    from uuid import uuid4

    from api.db.db_models import Knowledgebase

    env = rest_documents
    ids = [uuid4().hex for _ in range(105)]
    with env["engine"].begin() as db:
        db.execute(
            sa.insert(Knowledgebase),
            [
                {"id": identifier, "tenant_id": env["owners"][0], "created_by": env["owners"][0], "name": "pagination_" + identifier, "embd_id": "scratch", "parser_id": "naive", "parser_config": {}}
                for identifier in ids
            ],
        )
    expected = set(ids) | {env["datasets"][0]}
    before = snapshot(env)
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("rest_consumer_mcp_server", root / "mcp/server/server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connector = module.MultiRAGConnector(base_url=env["base"])
    result = asyncio.run(connector.list_datasets_structured(env["keys"][0]))
    assert len(result) == 106 and {row["id"] for row in result} == expected
    monkeypatch.syspath_prepend(str(root / "admin/client"))
    cli_module = importlib.import_module("multirag_client")
    http_module = importlib.import_module("http_client")
    address = urlsplit(env["base"])
    http = http_module.HttpClient(host=address.hostname, port=address.port)
    http.login_token = "Bearer " + env["jwt"][0]
    cli = cli_module.MultiRAGClient(http, "user")
    printed: list[Any] = []
    monkeypatch.setattr(cli, "_print_table_simple", printed.append)
    cli.list_user_datasets({})
    assert len(printed) == 1 and len(printed[0]) == 106
    assert {row["id"] for row in printed[0]} == expected
    assert snapshot(env) == before
    env["journal"].event("assertions", real_mcp_http_client=True, real_cli_http_client=True, accessible_datasets=106, foreign_excluded=True, no_read_side_effects=True)


def test_retrieval_document_scope_and_no_match_metadata_before_models(rest_documents: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    env = rest_documents
    doc = upload(env, ["retrieval_scope.txt"])[0]
    before = snapshot(env)
    sdk = sys.modules["api.apps.restful_apis.chunk"]

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid or empty filtered selection must not load a provider")

    monkeypatch.setattr(sdk, "LLMBundle", forbidden)
    payload = {"question": "chunk", "dataset_ids": [env["datasets"][0]]}
    response = call(env, "POST", "/retrieval", json={**payload, "document_ids": "bad"})
    assert response.status_code == 422 and snapshot(env) == before
    response = call(env, "POST", "/retrieval", json={**payload, "document_ids": ["not-owned"]})
    assert response.status_code == 200 and response.json() == {"code": 102, "message": "The datasets don't own the document not-owned"}
    response = call(
        env,
        "POST",
        "/retrieval",
        json={**payload, "document_ids": [doc["id"]], "metadata_condition": {"logic": "and", "conditions": [{"name": "author", "comparison_operator": "is", "value": "missing"}]}},
    )
    assert response.status_code == 200 and response.json() == {"code": 0, "data": {"total": 0, "chunks": [], "doc_aggs": {}}}
    assert snapshot(env) == before
    env["journal"].event("assertions", retrieval_scope_actual_http_sql=True, no_model_calls=True, no_read_side_effects=True)


def test_document_patch_name_config_metadata_and_strict_guards(rest_documents: dict[str, Any]) -> None:
    from api.db.db_models import DocumentMetadata
    from api.db.services.document_ingest_recovery import recovery_key

    env = rest_documents
    docs = upload(env, ["patch.txt", "duplicate.txt"])
    doc = docs[0]
    path = f"/datasets/{env['datasets'][0]}/documents/{doc['id']}"
    key = recovery_key(doc["id"])
    env["journal"].event("reserve-redis", key=key, document=doc["id"])
    assert not REDIS_CONN.REDIS.exists(key)
    try:
        response = call(
            env, "PATCH", path, json={"name": "renamed.txt", "meta_fields": {"author": "alice", "tags": ["one", "two"], "zero": 0}, "parser_config": {"auto_keywords": 0, "auto_questions": 0}}
        )
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        assert documents(env)[doc["id"]]["name"] == "renamed.txt"
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(DocumentMetadata.meta_fields).where(DocumentMetadata.id == doc["id"])) == {"author": "alice", "tags": ["one", "two"], "zero": 0}
        before = snapshot(env)
        for patch, status, code in [
            ({"name": None}, 422, 101),
            ({"name": 0}, 422, 101),
            ({"name": ""}, 422, 101),
            ({"parser_config": {"chunk_token_num": 0}}, 400, 102),
            ({"parser_config": {"auto_keywords": "1"}}, 422, 101),
            ({"parser_config": {"invalid_key": 1}}, 422, 101),
            ({"meta_fields": {"nested": {"x": 1}}}, 422, 101),
            ({"created_by": "other"}, 422, 101),
        ]:
            response = call(env, "PATCH", path, json=patch)
            assert response.status_code == status and response.json()["retcode"] == code, (patch, response.text)
            assert response.json()["code"] == ("DOCUMENT_UPDATE_INVALID" if status == 400 else "DOCUMENT_UPDATE_VALIDATION")
            assert snapshot(env) == before
        for credential in (None, "invalid-token", env["keys"][1]):
            response = call(env, "PATCH", path, credential=credential, json={"name": "denied.txt"})
            assert response.status_code == (404 if credential == env["keys"][1] else 401), response.text
            assert snapshot(env) == before
        env["journal"].event("assertions", patch_http_sql_roundtrip=True, strict_guards_no_write=True)
    finally:
        REDIS_CONN.REDIS.delete(key)
        absent = not REDIS_CONN.REDIS.exists(key)
        env["journal"].event("redis-cleanup", key=key, absent=absent)
        assert absent


@pytest.mark.parametrize("raw", [False, True])
def test_partial_upload_committed_inventory_and_failed_only_retry(rest_documents: dict[str, Any], raw: bool) -> None:
    env = rest_documents
    path = f"/datasets/{env['datasets'][0]}/documents"
    params = {"return_raw_files": str(raw).lower()}
    response = call(env, "POST", path, params=params, files=[("file", ("valid.txt", b"confirmed upload")), ("file", ("unsupported.exe", b"unsupported"))])
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 500 and payload["message"] == "unsupported.exe: This type of file has not been supported yet!"
    confirmed = payload["data"]["uploaded"]
    assert len(confirmed) == 1 and confirmed[0]["name"] == "valid.txt"
    assert confirmed[0]["dataset_id"] == env["datasets"][0] and confirmed[0]["run"] == "UNSTART"
    rows = documents(env)
    assert len(rows) == 1 and set(rows) == {confirmed[0]["id"]}
    stored = next(iter(rows.values()))
    assert read_object(env["storage"], env["bucket"], f"{stored['kb_id']}/{stored['location']}") == b"confirmed upload"
    before = snapshot(env)
    assert len(before["objects"]) == 1
    retried = call(env, "POST", path, params=params, files=[("file", ("unsupported.exe", b"unsupported"))])
    assert retried.status_code == 200 and retried.json() == {"code": 500, "message": payload["message"]}
    assert snapshot(env) == before
    for credential in (None, "invalid-token", env["keys"][1]):
        denied = call(env, "POST", path, credential=credential, params=params, files=[("file", ("denied.txt", b"must not upload")), ("file", ("unsupported.exe", b"unsupported"))])
        assert denied.status_code == (200 if credential == env["keys"][1] else 401)
        assert denied.json().get("code", denied.json().get("retcode")) == 109
        assert snapshot(env) == before
    env["journal"].event(
        "assertions",
        partial_error_code=500,
        confirmed_documents=1,
        confirmed_objects=1,
        retry_failed_only_no_duplicate=True,
        auth_denials_no_write=True,
        return_raw_files=raw,
        partial_confirmation="canonical",
    )
