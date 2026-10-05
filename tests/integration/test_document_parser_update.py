"""Owned listener and complete PostgreSQL/Milvus/MinIO/Redis PATCH readbacks."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, DocumentMetadata, File, File2Document, Knowledgebase, Task, UserCanvas, UserTenant
from api.db.services import document_image_lock as image_lock
from api.db.services import document_ingest_service as ingest_service
from api.db.services import document_parser_service as parser_service
from api.db.services import document_status_service as status_service
from api.db.services import document_task_service as task_service
from api.db.services.document_ingest_recovery import recovery_key
from common import resources, settings
from common.config_utils import CONFIGS
from common.doc_store.document_history import document_history
from core.utils.redis_conn import REDIS_CONN
from tests.support.document_image_http import image_http_api as image_http_api
from tests.support.document_image_read_service import image_resources as image_resources
from tests.support.document_parser_update import assert_durable_journal, assert_full_read_only, assert_nonparser_physical_allowlist, full_readback, patch, save, snapshot
from tests.support.document_parser_update import bootstrapped_engine as bootstrapped_engine
from tests.support.document_parser_update import parser_api as parser_api
from tests.support.document_parser_update import parser_database as parser_database


def _seed_legacy_metadata(env: dict[str, Any]) -> dict[str, Any]:
    with Session(env["engine"]) as db:
        doc = db.get(Document, env["docs"]["a"])
        assert doc is not None
        doc.parser_config = {
            **doc.parser_config,
            "metadata": [
                {"key": "a", "type": "number", "examples": ["2024", "2025.5"], "restrict_values": True, "description": "Year"},
                {"key": "b", "type": "string", "description": "Original b", "enum": ["one", "two"]},
            ],
            "built_in_metadata": [{"key": "filename"}],
        }
        db.commit()
    return _read_metadata_document(env)


def _read_metadata_document(env: dict[str, Any]) -> dict[str, Any]:
    response = requests.get(
        env["base"] + f"/api/v1/datasets/{env['ids']['kb']}/documents",
        params={"id": env["docs"]["a"]},
        headers={"Authorization": "Bearer " + env["tokens"]["owner"]},
        timeout=30,
    )
    assert response.status_code == 200 and response.json()["code"] == 0
    documents = response.json()["data"]["docs"]
    assert len(documents) == 1
    return documents[0]


def test_metadata_legacy_schema_patch_merges_latest_locked_snapshot(parser_api: dict[str, Any]) -> None:
    env = parser_api
    initial = _seed_legacy_metadata(env)
    assert set(initial["parser_config"]["metadata"]["properties"]) == {"a", "b"}
    assert initial["parser_config"]["metadata"]["properties"]["a"]["enum"] == [2024, 2025.5]
    # Both editors first read the historical array as Schema. The second edit
    # arrives after b commits; its raw partial patch must use the latest b.
    patch(env, {"parser_config": {"metadata": {"properties": {"b": {"description": "Edited b"}}}}})
    result = patch(env, {"parser_config": {"metadata": {"properties": {"a": {"description": "Edited a"}}}}})["data"]
    listed = _read_metadata_document(env)
    with Session(env["engine"]) as db:
        stored = db.get(Document, env["docs"]["a"]).parser_config
        assert result["parser_config"] == listed["parser_config"] == stored
        assert stored["metadata"]["properties"] == {
            "a": {"type": "number", "description": "Edited a", "enum": [2024, 2025.5]},
            "b": {"type": "string", "description": "Edited b", "enum": ["one", "two"]},
        }
        assert stored["built_in_metadata"] == [{"key": "filename"}] and stored["unknown"] == [0]
    for payload in [{"parser_config": {}}, {"parser_config": {"metadata": {}}}, {"parser_config": {"metadata": {"properties": {}}}}]:
        assert patch(env, payload)["data"]["parser_config"] == stored
    replacement = {"type": "object", "properties": {"a": {"type": "number", "enum": [1, 2.5]}}}
    response = requests.put(
        env["base"] + f"/api/v1/datasets/{env['ids']['kb']}/documents/{env['docs']['a']}/metadata/config",
        json={"metadata": replacement},
        headers={"Authorization": "Bearer " + env["tokens"]["owner"]},
        timeout=30,
    )
    assert response.status_code == 200 and response.json()["code"] == 0
    assert _read_metadata_document(env)["parser_config"]["metadata"] == replacement
    with Session(env["engine"]) as db:
        assert db.get(Document, env["docs"]["a"]).parser_config["metadata"] == replacement
    assert patch(env, {"parser_config": {"metadata": [{"key": "b", "type": "string"}]}})["data"]["parser_config"]["metadata"] == [{"key": "b", "type": "string"}]
    assert set(_read_metadata_document(env)["parser_config"]["metadata"]["properties"]) == {"b"}
    cleared = patch(env, {"parser_config": {"metadata": []}})["data"]["parser_config"]
    assert cleared["metadata"] == [] and cleared["built_in_metadata"] == [{"key": "filename"}]
    assert _read_metadata_document(env)["parser_config"] == cleared
    with Session(env["engine"]) as db:
        assert db.get(Document, env["docs"]["a"]).parser_config == cleared


def test_metadata_overlapping_edit_conflict_then_raw_patch_retry_preserves_both(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    env = parser_api
    _seed_legacy_metadata(env)
    entered, selected, release = threading.Event(), threading.Event(), threading.Event()
    original_set = ingest_service._set_document
    original_preflight = parser_service.preflight_document_update

    def hold(db: Session, doc: Document, kb: Knowledgebase, values: dict[str, Any]) -> None:
        if doc.id == env["docs"]["a"] and values.get("parser_config", {}).get("metadata", {}).get("properties", {}).get("a", {}).get("description") == "Concurrent a":
            entered.set()
            assert release.wait(20)
        original_set(db, doc, kb, values)

    def preflight(*args: Any, **kwargs: Any) -> Any:
        selection = original_preflight(*args, **kwargs)
        if args[4].model_dump(exclude_unset=True).get("parser_config", {}).get("metadata", {}).get("properties", {}).get("b", {}).get("description") == "Concurrent b":
            selected.set()
        return selection

    first_patch = {"parser_config": {"metadata": {"properties": {"a": {"description": "Concurrent a"}}}}}
    second_patch = {"parser_config": {"metadata": {"properties": {"b": {"description": "Concurrent b"}}}}}
    with monkeypatch.context() as scope:
        scope.setattr(ingest_service, "_set_document", hold)
        scope.setattr(parser_service, "preflight_document_update", preflight)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(patch, env, first_patch)
            assert entered.wait(10)
            second = pool.submit(patch, env, second_patch, status=409)
            try:
                assert selected.wait(10)
            finally:
                release.set()
            assert first.result(20)["code"] == 0
            conflict = second.result(20)
            assert conflict["code"] == "DOCUMENT_UPDATE_CONFLICT" and conflict["retcode"] == 102 and conflict["data"] == {"outcome": "unchanged"}
    assert _read_metadata_document(env)["parser_config"]["metadata"]["properties"]["b"]["description"] == "Original b"
    retried = patch(env, second_patch)["data"]["parser_config"]
    assert retried["metadata"]["properties"]["a"]["description"] == "Concurrent a"
    assert retried["metadata"]["properties"]["b"]["description"] == "Concurrent b"
    assert retried["metadata"]["properties"]["a"]["enum"] == [2024, 2025.5]
    assert _read_metadata_document(env)["parser_config"] == retried
    with Session(env["engine"]) as db:
        assert db.get(Document, env["docs"]["a"]).parser_config == retried


@pytest.mark.parametrize("role,key,status", [("normal", "a", 403), ("outsider", "a", 404), ("owner", "foreign", 404)])
def test_metadata_schema_patch_still_requires_document_ownership(parser_api: dict[str, Any], role: str, key: str, status: int) -> None:
    env = parser_api
    _seed_legacy_metadata(env)
    before = snapshot(env)
    result = patch(env, {"parser_config": {"metadata": {"properties": {"a": {"description": "Unauthorized"}}}}}, role=role, key=key, status=status)
    assert result["code"] == ("DOCUMENT_UPDATE_FORBIDDEN" if status == 403 else "DOCUMENT_UPDATE_UNAVAILABLE")
    assert result["retcode"] == (109 if status == 403 else 102) and result["data"] == {"outcome": "unchanged"}
    assert snapshot(env) == before


def test_retired_parser_real_http_fullstores_zero_private_calls(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.parse import unquote, urlsplit

    from pymilvus import MilvusClient

    from api.db.db_models import APIToken
    from api.db.services.file_service import FileService

    env = parser_api
    env["tokens"]["owner_api"] = "parser-exit-" + uuid4().hex
    env["tokens"]["invalid_literal"] = "INVALID_"
    env["parser_record"]["manifest"]["oldparser_api_token"] = {"tenant_id": env["ids"]["owner"], "role": "owner_api"}
    save(env["parser_record_path"], env["parser_record"])
    with Session(env["engine"]) as db:
        db.add(APIToken(tenant_id=env["ids"]["owner"], token=env["tokens"]["owner_api"], name="image-http"))
        db.commit()
    group, consumer = "oldparser-retirement", "oldparser-reader"
    env["parser_record"]["manifest"]["oldparser_stream_group"] = {"key": env["queue"], "group": group, "consumer": consumer}
    save(env["parser_record_path"], env["parser_record"])
    assert REDIS_CONN.queue_product(env["queue"], {"id": env["parser_record"]["manifest"]["tasks"][0], "doc_id": env["docs"]["a"], "preserved": True})
    REDIS_CONN.REDIS.xgroup_create(env["queue"], group, id="0")
    REDIS_CONN.REDIS.xreadgroup(group, consumer, {env["queue"]: ">"}, count=1)
    cfg = CONFIGS["milvus"]
    native = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
    try:
        for collection in env["collections"].values():
            if native.has_collection(collection):
                native.flush(collection, timeout=30)
    finally:
        native.close()
    valid = {"doc_id": env["docs"]["a"], "parser_id": "paper", "parser_config": {"chunk_token_num": 512}}
    cases: dict[str, list[tuple[str, str, str | None, dict[str, Any]]]] = {
        "credentials": [
            ("POST", "/v1/document/change_parser", role, {"json": {**valid, "doc_id": env["docs"]["foreign"] if role == "api" else env["docs"]["a"]}})
            for role in ["owner", "admin", "normal", "invite", "inactive", "outsider", "other", "owner_api", "api", "disabled", "expired", "malformed", "unknown", "invalid_literal", None]
        ],
        "bodies_and_paths": [
            ("POST", "/v1/document/change_parser", "owner", {"json": body})
            for body in [
                {},
                None,
                [],
                {"doc_id": uuid4().hex},
                {"doc_id": env["docs"]["foreign"]},
                {"doc_id": env["docs"]["a"], "pipeline_id": env["canvas"]},
                {"doc_id": env["docs"]["a"], "pipeline_id": ""},
                {"doc_id": None},
                {"doc_id": 1},
                {"parser_config": []},
                {"parser_config": None},
                {"doc_id": env["docs"]["a"], "extra": True},
            ]
        ]
        + [
            ("POST", path, "owner", {"json": valid, "params": {"doc_id": env["docs"]["foreign"], "owner": env["ids"]["other"]}})
            for path in ["/v1/document/change_parser/", "/v1/document/%63hange_parser", "/v1/document/change_parser/%25汉字"]
        ]
        + [("POST", "/v1/document/change_parser", "owner", {"data": raw, "headers": {"Content-Type": "application/json"}}) for raw in [b'{"invalid":', b"null", b'{"parser_config": {"score": NaN}}']],
        "methods": [(method, "/v1/document/change_parser", "owner", {"json": valid}) for method in ["GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]],
    }
    record: dict[str, Any] = {"groups": {}, "private_calls": [], "auth": "real JWT/API tokens; no principal override", "redirects": "disabled"}
    evidence = env["evidence"] / f"{env['ids']['kb']}.oldparser-retirement.json"
    save(evidence, record)
    for label, requests_in_group in cases.items():
        observation: dict[str, Any] = {"before": full_readback(env), "requests": []}
        record["groups"][label] = observation
        start = time.monotonic()

        def forbid(*args: Any, **kwargs: Any) -> Any:
            record["private_calls"].append("private parser/storage/queue")
            save(evidence, record)
            raise AssertionError("retired parser reached private service")

        def sql_guard(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
            record["private_calls"].append({"sql": statement})
            save(evidence, record)
            raise AssertionError("retired parser reached business SQL")

        with monkeypatch.context() as guard:
            for module in [parser_service, sys.modules["api.apps.restful_apis.document"]]:
                guard.setattr(module, "update_document_parser", forbid)
            for target, attributes in [
                (env["storage"], ["get", "get_bytes", "put", "rm"]),
                (settings.docStoreConn, ["search", "insert", "update", "delete"]),
                (REDIS_CONN, ["queue_product"]),
                (FileService, ["parse", "parse_docs", "upload_info", "upload_infos"]),
                (status_service, ["prepare_source_recovery"]),
                (ingest_service, ["_operate"]),
            ]:
                for attr in attributes:
                    guard.setattr(target, attr, forbid)
            engines = [env["engine"], env["async_engine"].sync_engine]
            for engine in engines:
                sa.event.listen(engine, "before_cursor_execute", sql_guard)
            try:
                for method, path, role, payload in requests_in_group:
                    params = dict(payload)
                    headers = params.pop("headers", {})
                    if role:
                        headers["Authorization"] = "Bearer " + env["tokens"][role]
                    response = requests.request(method, env["base"] + path, headers=headers, timeout=30, allow_redirects=False, **params)
                    actual_path = unquote(urlsplit(response.request.url).path)
                    item = {
                        "method": method,
                        "path": path,
                        "actual_path": actual_path,
                        "url": response.request.url,
                        "role": role,
                        "request_payload": payload,
                        "status": response.status_code,
                        "headers": dict(response.headers),
                        "raw": response.content,
                    }
                    observation["requests"].append(item)
                    save(evidence, record)
                    assert response.status_code == 404 and "location" not in response.headers and response.headers["content-type"] == "application/json"
                    if method == "HEAD":
                        assert response.content == b""
                    else:
                        assert response.json() == {"code": 404, "message": "Not Found: " + actual_path, "data": None, "error": "Not Found"}
                if label == "methods":
                    response = requests.options(
                        env["base"] + "/v1/document/change_parser", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"}, timeout=30, allow_redirects=False
                    )
                    observation["cors_preflight"] = {
                        "status": response.status_code,
                        "headers": dict(response.headers),
                        "raw": response.content,
                        "classification": "existing CORS middleware; not counted as route404",
                    }
                    assert response.status_code in {200, 400} and "access-control-allow-methods" in response.headers
            finally:
                for engine in engines:
                    sa.event.remove(engine, "before_cursor_execute", sql_guard)
        observation["after"] = full_readback(env)
        observation["elapsed"] = time.monotonic() - start
        save(evidence, record)
        assert_full_read_only(observation["before"], observation["after"], observation["elapsed"])
        observation["unchanged_except_exact_bounded_relative_age"] = True
        save(evidence, record)
    assert not record["private_calls"]
    record["verified_requests"] = sum(len(item["requests"]) for item in record["groups"].values())
    paths = requests.get(env["base"] + "/openapi.json", timeout=30, allow_redirects=False).json()
    record["openapi"] = paths
    assert "/v1/document/change_parser" not in paths["paths"]
    assert "ChangeParserRequest" not in paths["components"]["schemas"] and "LegacyDocumentParserPatch" not in paths["components"]["schemas"]
    for payload in [{}, {"chunk_method": "naive"}, {"parser_config": {}}, {"parser_config": {"raptor": {"use_raptor": False}}}]:
        before = full_readback(env)
        data = patch(env, payload)["data"]
        after = full_readback(env)
        assert data["run"] == "DONE" and data["enabled"] is False and data["chunk_count"] == 2
        assert_nonparser_physical_allowlist(env, before, after, payload)
        for name in ["index", "objects", "native_objects", "queue", "redis", "image_reservations"]:
            assert before[name] == after[name]
    save(evidence, record)


def test_real_http_save_same_config_and_reset_exact_images(parser_api: dict[str, Any]) -> None:
    env = parser_api
    original = snapshot(env)
    for payload in [{}, {"chunk_method": "naive"}, {"parser_config": {}}, {"parser_config": {"chunk_token_num": 8192, "raptor": {"use_raptor": False}}}]:
        data = patch(env, payload)["data"]
        assert data["state"] == data["run"] == "DONE" and data["enabled"] is False
        assert data["pipeline_id"] is None
        assert data["parser_config"]["unknown"] == [0] and data["parser_config"]["raptor"]["future"] is None
        current = snapshot(env)
        assert current["index"] == original["index"] and current["objects"] == original["objects"] and current["queue"] == original["queue"]
        assert current["sql"][Task.__tablename__] == original["sql"][Task.__tablename__]
    data = patch(env, {"pipeline_id": env["canvas"]})["data"]
    assert data["pipeline_id"] == env["canvas"] and data["chunk_method"] == "naive"
    assert data["state"] == data["run"] == "UNSTART" and data["enabled"] is False and data["chunk_count"] == data["token_count"] == 0
    current = snapshot(env)
    assert not document_history(settings.docStoreConn, env["collections"]["kb"], env["ids"]["kb"], env["docs"]["a"])
    assert env["storage"].get_bytes(env["ids"]["kb"], "history.png") is None
    for namespace, key in [(env["ids"]["kb"], "a.txt"), (env["ids"]["kb"], "shared.png"), (env["ids"]["foreign"], "foreign.png")]:
        assert env["storage"].get_bytes(namespace, key) is not None
    assert current["index"]["foreign"] == original["index"]["foreign"] and current["queue"] is None
    assert all(row["doc_id"] != env["docs"]["a"] for row in current["sql"][Task.__tablename__])
    for payload in [
        {"pipeline_id": env["canvas"], "parser_config": {"delimiter": ";"}},
        {"parser_config": {"metadata": {"properties": {"year": {"type": "integer"}}}}},
        {"name": "renamed.txt", "meta_fields": {"score": 0}},
    ]:
        assert patch(env, payload)["data"]["pipeline_id"] == env["canvas"]
    assert patch(env, {"pipeline_id": ""})["data"]["pipeline_id"] == ""
    assert patch(env, {"chunk_method": "paper"})["data"]["chunk_method"] == "paper"
    smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, capture_output=True, text=True, timeout=60)
    env["parser_record_path"].with_suffix(".smoke.log").write_text(smoke.stdout + smoke.stderr + f"\nexit={smoke.returncode}\n")
    assert smoke.returncode == 0


@pytest.mark.parametrize(
    "role,status", [(None, 401), ("normal", 403), ("invite", 403), ("inactive", 404), ("outsider", 404), ("disabled", 401), ("expired", 401), ("malformed", 401), ("unknown", 401)]
)
def test_real_http_authorization_and_no_effects(parser_api: dict[str, Any], role: str | None, status: int) -> None:
    before = snapshot(parser_api)
    patch(parser_api, {"pipeline_id": parser_api["canvas"], "name": "invalid-mixed.txt", "enabled": True}, role=role, status=status)
    assert snapshot(parser_api) == before


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"pipeline_id": None}, 422),
        ({"chunk_method": "general"}, 400),
        ({"pipeline_id": "INVALID"}, 400),
        ({"pipeline_id": "a" * 32, "chunk_method": "paper"}, 400),
        ({"parser_config": {"ext": {}}}, 422),
        ({"parser_config": {"chunk_token_num": "512"}}, 422),
        ({"parser_config": {"chunk_token_num": 8193}}, 400),
        ({"parser_config": {"pages": [[3, 2]]}}, 400),
        ({"chunk_count": 123}, 400),
        ({"meta_fields": {"nested": {"key": "secret"}}}, 422),
    ],
)
def test_real_http_invalid_mixed_request_zero_writes(parser_api: dict[str, Any], payload: dict[str, Any], status: int) -> None:
    before = snapshot(parser_api)
    patch(parser_api, {"name": "new.txt", "enabled": True, "meta_fields": {"a": 1}, **payload}, status=status)
    assert snapshot(parser_api) == before


@pytest.mark.parametrize("fault", ["delete_false", "delete_raise", "delete_remaining", "delete_wrong_ack", "object_false", "object_raise", "object_remaining", "late_sql", "metadata", "commit_before"])
def test_faults_restore_full_sql_native_vectors_and_exact_image_bytes(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    from api.db.services.metadata_store_sql import SqlMetadataStore

    env = parser_api
    before = snapshot(env)
    once = False
    original_delete = settings.docStoreConn.delete
    original_rm = env["storage"].rm
    original_set = ingest_service._set_document

    def store_delete(*args: Any, **kwargs: Any) -> Any:
        nonlocal once
        if not once:
            once = True
            if fault == "delete_raise":
                original_delete(*args, **kwargs)
                raise OSError("Controlled delete response failure")
            return False if fault == "delete_false" else 0 if fault == "delete_remaining" else True
        return original_delete(*args, **kwargs)

    def object_rm(*args: Any, **kwargs: Any) -> Any:
        nonlocal once
        if not once:
            once = True
            if fault == "object_raise":
                original_rm(*args, **kwargs)
                raise OSError("Controlled object response failure")
            return False if fault == "object_false" else None
        return original_rm(*args, **kwargs)

    def set_document(db: Session, doc: Document, kb: Knowledgebase, values: dict[str, Any]) -> None:
        nonlocal once
        if not once and doc.id == env["docs"]["a"]:
            once = True
            raise RuntimeError("Controlled late SQL failure")
        original_set(db, doc, kb, values)

    def metadata(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Controlled metadata failure")

    def before_commit(db: Session) -> None:
        nonlocal once
        if db.bind is env["engine"] and not once and db.scalar(sa.select(Document.name).where(Document.id == env["docs"]["a"])) == "fault-new.txt":
            once = True
            raise RuntimeError("Controlled COMMIT boundary")

    with monkeypatch.context() as fault_scope:
        if fault.startswith("delete_"):
            fault_scope.setattr(settings.docStoreConn, "delete", store_delete)
        elif fault.startswith("object_"):
            fault_scope.setattr(env["storage"], "rm", object_rm)
        elif fault == "late_sql":
            fault_scope.setattr(ingest_service, "_set_document", set_document)
        elif fault == "metadata":
            fault_scope.setattr(SqlMetadataStore, "upsert_in_transaction", metadata)
        elif fault == "commit_before":
            sa.event.listen(Session, "before_commit", before_commit)
        try:
            body = patch(env, {"chunk_method": "paper", "name": "fault-new.txt", "enabled": True, "meta_fields": {"score": 1}}, status=500)
        finally:
            if fault == "commit_before":
                sa.event.remove(Session, "before_commit", before_commit)
    assert body["code"] == "DOCUMENT_UPDATE_FAILED" and body["details"]["outcome"] == "unchanged"
    assert snapshot(env) == before
    assert not REDIS_CONN.REDIS.exists(recovery_key(env["docs"]["a"]))


def test_failed_restore_retains_durable_material_and_retry_restores_exact_state(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = parser_api
    before = snapshot(env)
    original_set = ingest_service._set_document
    original_insert = settings.docStoreConn.insert

    def late_sql(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Controlled SQL failure")

    def restore_fail(*args: Any, **kwargs: Any) -> list[str]:
        return ["Controlled full-history restore failure"]

    with monkeypatch.context() as failure:
        failure.setattr(ingest_service, "_set_document", late_sql)
        failure.setattr(settings.docStoreConn, "insert", restore_fail)
        body = patch(env, {"chunk_method": "paper", "name": "fault-new.txt", "enabled": True}, status=500)
    assert body["code"] == "DOCUMENT_UPDATE_OUTCOME_UNKNOWN" and body["details"]["outcome"] == "unknown"
    assert REDIS_CONN.REDIS.exists(recovery_key(env["docs"]["a"]))
    assert_durable_journal(env)
    assert env["storage"].get_bytes(env["ids"]["kb"], "history.png") is None
    assert original_set is ingest_service._set_document and original_insert == settings.docStoreConn.insert
    patch(env, {"parser_config": {}})
    after = snapshot(env)
    for field in ["index", "objects", "queue", "redis"]:
        assert after[field] == before[field]
    for model in [Knowledgebase, Task, File, File2Document, DocumentMetadata]:
        assert after["sql"][model.__tablename__] == before["sql"][model.__tablename__]
    assert not REDIS_CONN.REDIS.exists(recovery_key(env["docs"]["a"]))


def test_rename_and_enabled_preserve_full_native_history_and_shared_sources(parser_api: dict[str, Any]) -> None:
    env = parser_api
    before = snapshot(env)
    body = patch(env, {"name": "renamed.txt", "enabled": True, "meta_fields": {"score": 0, "tags": ["a,b"]}})
    data = body["data"]
    assert data["enabled"] is True and data["state"] == "DONE" and data["chunk_count"] == 2
    assert data["pipeline_id"] is None and data["meta_fields"] == {"score": 0, "tags": ["a", "b"]}
    current = snapshot(env)
    expected = copy.deepcopy(before["index"]["kb"]["rows"])
    from core.nlp import rag_tokenizer

    title = rag_tokenizer.tokenize("renamed.txt")
    for row in expected:
        if row["doc_id"] == env["docs"]["a"]:
            row.update(docnm_kwd="renamed.txt", title_tks=title, title_sm_tks=rag_tokenizer.fine_grained_tokenize(title), available_int=0 if row["id"].endswith("mother") else 1)
    assert current["index"]["kb"]["rows"] == expected
    assert current["index"]["foreign"] == before["index"]["foreign"] and current["objects"] == before["objects"] and current["queue"] == before["queue"]
    assert current["sql"][Task.__tablename__] == before["sql"][Task.__tablename__]
    with Session(env["engine"]) as db:
        assert db.get(File, env["parser_record"]["manifest"]["files"][0]).name == "renamed.txt"
    assert patch(env, {"enabled": False})["data"]["enabled"] is False


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_canonical_patch_same_mode_config_clear_and_pipeline_presence(parser_api: dict[str, Any], role: str) -> None:
    env = parser_api
    before = snapshot(env)
    data = patch(env, {"chunk_method": "naive", "parser_config": {"chunk_token_num": 512}}, role=role)["data"]
    assert data["chunk_method"] == "naive" and data["parser_config"]["chunk_token_num"] == 512
    assert snapshot(env)["index"] == before["index"] and snapshot(env)["objects"] == before["objects"]
    assert patch(env, {"pipeline_id": env["canvas"]}, role=role)["data"]["pipeline_id"] == env["canvas"]
    data = patch(env, {"pipeline_id": "", "parser_config": {"delimiter": ";"}}, role=role)["data"]
    assert data["pipeline_id"] == "" and data["chunk_method"] == "naive" and data["parser_config"]["delimiter"] == ";" and data["status"] == "0"
    before = snapshot(env)
    assert patch(env, {"pipeline_id": None}, role=role, status=422)["code"] == "DOCUMENT_UPDATE_VALIDATION"
    assert snapshot(env) == before


def test_canonical_patch_preserves_unknown_finalization_and_recovers(parser_api: dict[str, Any]) -> None:
    env = parser_api
    before = snapshot(env)
    once = False

    def before_commit(db: Session) -> None:
        nonlocal once
        if db.bind is env["engine"] and not once and db.scalar(sa.select(Document.parser_id).where(Document.id == env["docs"]["a"])) == "paper":
            db.info["parser_lost_commit_response"] = True
            once = True

    def after_commit(db: Session) -> None:
        if db.info.pop("parser_lost_commit_response", False):
            raise RuntimeError("Controlled parser lost COMMIT response")

    sa.event.listen(Session, "before_commit", before_commit)
    sa.event.listen(Session, "after_commit", after_commit)
    try:
        body = patch(env, {"chunk_method": "paper"}, status=500)
    finally:
        sa.event.remove(Session, "before_commit", before_commit)
        sa.event.remove(Session, "after_commit", after_commit)
    assert body["code"] == "DOCUMENT_UPDATE_OUTCOME_UNKNOWN" and body["retcode"] == 500 and body["data"] == body["details"] == {"outcome": "unknown"}
    assert REDIS_CONN.REDIS.exists(recovery_key(env["docs"]["a"]))
    assert_durable_journal(env)
    with Session(env["engine"]) as db:
        assert db.get(Document, env["docs"]["a"]).parser_id == "paper"
    data = patch(env, {"parser_config": {}})["data"]
    assert data["chunk_method"] == "naive" and data["run"] == "DONE" and data["chunk_count"] == 2 and data["enabled"] is False
    after = snapshot(env)
    for name in ["index", "objects", "queue"]:
        assert after[name] == before[name]
    for name in before["sql"]:
        if name != Document.__tablename__:
            assert after["sql"][name] == before["sql"][name], name
    assert not REDIS_CONN.REDIS.exists(recovery_key(env["docs"]["a"]))


@pytest.mark.parametrize("change", ["deleted", "private", "category", "dsl", "foreign_owner"])
def test_actual_canvas_lookup_window_revalidation_preserves_other_state(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    env = parser_api
    original = parser_service.save_document_update
    expected: dict[str, Any] = {}

    def window(*args: Any) -> Any:
        with Session(env["engine"]) as db:
            if change == "deleted":
                db.execute(sa.delete(UserCanvas).where(UserCanvas.id == env["canvas"]))
            else:
                values = (
                    {"permission": "me"}
                    if change == "private"
                    else {"canvas_category": "agent_canvas"}
                    if change == "category"
                    else {"dsl": {}}
                    if change == "dsl"
                    else {"user_id": env["ids"]["other"]}
                )
                db.execute(sa.update(UserCanvas).where(UserCanvas.id == env["canvas"]).values(**values))
            db.commit()
        expected.update(snapshot(env))
        return original(*args)

    monkeypatch.setattr(parser_service, "save_document_update", window)
    patch(env, {"pipeline_id": env["canvas"], "enabled": True, "name": "new.txt", "meta_fields": {"score": 1}}, role="admin", status=400 if change in {"category", "dsl"} else 404)
    assert snapshot(env) == expected


@pytest.mark.parametrize("change", ["inactive_kb", "inactive_member", "removed_member", "normal_member"])
def test_document_authorization_window_preserves_other_state(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    env = parser_api
    original = parser_service.save_document_update
    expected: dict[str, Any] = {}

    def window(*args: Any) -> Any:
        with Session(env["engine"]) as db:
            member = (UserTenant.tenant_id == env["ids"]["owner"]) & (UserTenant.user_id == env["ids"]["admin"])
            if change == "inactive_kb":
                db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["ids"]["kb"]).values(status="0"))
            elif change == "removed_member":
                db.execute(sa.delete(UserTenant).where(member))
            else:
                db.execute(sa.update(UserTenant).where(member).values(**({"status": "0"} if change == "inactive_member" else {"role": "normal"})))
            db.commit()
        expected.update(snapshot(env))
        return original(*args)

    monkeypatch.setattr(parser_service, "save_document_update", window)
    body = patch(env, {"chunk_method": "paper", "name": "new.txt", "enabled": True, "meta_fields": {"score": 1}}, role="admin", status=403 if change == "normal_member" else 404)
    assert body["code"] == ("DOCUMENT_UPDATE_FORBIDDEN" if change == "normal_member" else "DOCUMENT_UPDATE_UNAVAILABLE")
    assert snapshot(env) == expected


def test_actual_deferred_postgresql_commit_rolls_back_all_effects(parser_api: dict[str, Any]) -> None:
    env = parser_api
    name = "parser_fault_" + uuid4().hex
    manifest = env["parser_record"]["manifest"]
    manifest["sql_functions"], manifest["sql_triggers"] = ["usr_ai." + name], [name]
    save(env["parser_record_path"], env["parser_record"])
    with env["engine"].begin() as db:
        db.execute(
            sa.text(
                f"CREATE FUNCTION usr_ai.{name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.id = '{env['docs']['a']}' AND NEW.parser_id = 'paper' THEN RAISE EXCEPTION 'controlled deferred parser failure'; END IF; RETURN NEW; END $$"
            )
        )
        db.execute(sa.text(f"CREATE CONSTRAINT TRIGGER {name} AFTER UPDATE ON usr_ai.t_ai_documents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION usr_ai.{name}()"))
    before = snapshot(env)
    try:
        body = patch(env, {"chunk_method": "paper", "name": "new.txt", "enabled": True, "meta_fields": {"score": 1}}, status=500)
        assert body["details"]["outcome"] == "unchanged" and snapshot(env) == before
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.text(f"DROP TRIGGER {name} ON usr_ai.t_ai_documents"))
            db.execute(sa.text(f"DROP FUNCTION usr_ai.{name}()"))
        with env["engine"].connect() as db:
            assert db.scalar(sa.text("SELECT count(*) FROM pg_trigger WHERE tgname=:name"), {"name": name}) == 0
            assert db.scalar(sa.text("SELECT count(*) FROM pg_proc WHERE proname=:name"), {"name": name}) == 0
    assert patch(env, {"chunk_method": "paper"})["data"]["chunk_method"] == "paper"


def test_same_document_conflict_and_independent_document_ledger_progress(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    env = parser_api
    # Independent document operations use disjoint image keys. A shared key is
    # deliberately serialized, covered by the exact-key window tests below.
    key = "independent-b.png"
    env["parser_record"]["manifest"]["objects"].append((env["ids"]["kb"], key))
    save(env["parser_record_path"], env["parser_record"])
    env["storage"].put(env["ids"]["kb"], key, b"Independent document image bytes")
    assert settings.docStoreConn.update({"doc_id": env["docs"]["b"]}, {"img_id": env["ids"]["kb"] + "-" + key}, env["collections"]["kb"], env["ids"]["kb"]) is True
    entered, release = threading.Event(), threading.Event()
    original_set = ingest_service._set_document

    def hold(db: Session, doc: Document, kb: Knowledgebase, values: dict[str, Any]) -> None:
        if doc.id == env["docs"]["a"] and values.get("name") == "first.txt":
            entered.set()
            assert release.wait(20)
        original_set(db, doc, kb, values)

    monkeypatch.setattr(ingest_service, "_set_document", hold)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(patch, env, {"chunk_method": "paper", "name": "first.txt"})
        assert entered.wait(10)
        second = pool.submit(patch, env, {"chunk_method": "book", "name": "second.txt"}, status=409)
        try:
            other = pool.submit(patch, env, {"chunk_method": "paper"}, key="b")
            assert other.result(10)["code"] == 0
            assert not first.done()
        finally:
            release.set()
        assert first.result(10)["code"] == 0
        assert second.result(10)["code"] == "DOCUMENT_UPDATE_CONFLICT"
    with Session(env["engine"]) as db:
        assert db.get(Document, env["docs"]["a"]).name == "first.txt"
        kb = db.get(Knowledgebase, env["ids"]["kb"])
        assert (kb.chunk_num, kb.token_num) == (0, 0)
    assert REDIS_CONN.REDIS.xlen(env["queue"]) == 0


def test_actual_http_repeated_cancellation_drains_owned_patch_work(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    env = parser_api
    entered, release, captured = threading.Event(), threading.Event(), threading.Event()
    handler: dict[str, Any] = {}
    original_finish, original_set = parser_service.finish_status_write, ingest_service._set_document

    async def finish(work: Any) -> Any:
        handler.update(loop=asyncio.get_running_loop(), task=asyncio.current_task())
        captured.set()
        return await original_finish(work)

    def hold(*args: Any, **kwargs: Any) -> None:
        entered.set()
        assert release.wait(15)
        original_set(*args, **kwargs)

    monkeypatch.setattr(parser_service, "finish_status_write", finish)
    monkeypatch.setattr(ingest_service, "_set_document", hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            requests.patch,
            env["base"] + f"/api/v1/datasets/{env['ids']['kb']}/documents/{env['docs']['a']}",
            json={"chunk_method": "paper"},
            headers={"Authorization": "Bearer " + env["tokens"]["owner"]},
            timeout=30,
        )
        try:
            assert entered.wait(10) and captured.wait(5)
            for _ in range(2):
                with Session(env["engine"]) as db:
                    with pytest.raises(sa.exc.OperationalError):
                        db.scalar(sa.select(Document).where(Document.id == env["docs"]["a"]).with_for_update(nowait=True))
                delivered = threading.Event()
                handler["loop"].call_soon_threadsafe(handler["task"].cancel)
                handler["loop"].call_soon_threadsafe(delivered.set)
                assert delivered.wait(3) and not future.done()
        finally:
            release.set()
        assert future.result(10).status_code == 500
    with Session(env["engine"]) as db:
        doc = db.scalar(sa.select(Document).where(Document.id == env["docs"]["a"]).with_for_update(nowait=True))
        assert doc.parser_id == "paper" and doc.run == "0" and doc.chunk_num == doc.token_num == 0
    assert not REDIS_CONN.REDIS.exists(recovery_key(env["docs"]["a"])) and REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    env["parser_record"]["cancelled_http_owned_drain"] = snapshot(env)
    save(env["parser_record_path"], env["parser_record"])


def test_saved_pipeline_and_builtin_reenter_real_ingest_source_and_worker_chain(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import hashlib
    from types import SimpleNamespace

    import numpy as np

    from api.db.services.task_service import TaskService
    from core.flow.pipeline import Pipeline
    from core.flow.tokenizer.tokenizer import Tokenizer
    from core.svr import task_executor as worker

    env = parser_api
    seen: list[dict[str, Any]] = []
    original_run = Pipeline.run

    async def run(canvas: Pipeline, *args: Any, **kwargs: Any) -> Any:
        assert canvas._tenant_id == env["ids"]["owner"] and canvas._source_document_id == env["docs"]["a"]
        try:
            result = await original_run(canvas, *args, **kwargs)
            seen.append({"path": list(canvas.path), "output": copy.deepcopy(result)})
            return result
        finally:
            canvas._thread_pool.shutdown(wait=True)

    async def embedding(tokenizer: Tokenizer, name: str, chunks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        assert name == "a.txt" and chunks and any("Original source a" in str(chunk) for chunk in chunks)
        seen.append({"source_name": name, "chunks": copy.deepcopy(chunks)})
        for chunk in chunks:
            chunk["q_768_vec"] = [0.2] * 768
        return chunks, 7

    monkeypatch.setattr(Pipeline, "run", run)
    monkeypatch.setattr(Tokenizer, "_embedding", embedding)
    assert patch(env, {"pipeline_id": env["canvas"]})["data"]["state"] == "UNSTART"

    def submit() -> dict[str, Any]:
        response = requests.post(env["base"] + "/api/v1/documents/ingest", json={"doc_ids": [env["docs"]["a"]], "run": 1}, headers={"Authorization": "Bearer " + env["tokens"]["owner"]}, timeout=30)
        assert response.status_code == 200 and response.json() == {"code": 0, "message": "success", "data": True}
        with Session(env["engine"]) as db:
            tasks = list(db.scalars(sa.select(Task).where(Task.doc_id == env["docs"]["a"])))
            assert len(tasks) == 1
            task = TaskService.get_task(db, tasks[0].id)
            assert task is not None
            return task

    task = submit()
    messages = [json.loads(fields.get(b"message", fields.get("message"))) for _, fields in REDIS_CONN.REDIS.xrange(env["queue"])]
    assert messages[-1]["tenant_id"] == env["ids"]["owner"] and messages[-1]["dataflow_id"] == env["canvas"] and messages[-1]["file"] is None
    with Session(env["engine"]) as db:
        doc, canvas = db.get(Document, env["docs"]["a"]), db.get(UserCanvas, env["canvas"])
        expected_digest = hashlib.sha256(json.dumps([canvas.dsl, doc.parser_config, doc.name], sort_keys=True, default=str).encode()).hexdigest()
        assert db.get(Task, task["id"]).digest == expected_digest
    manifest = env["parser_record"]["manifest"]
    manifest["redis_keys"].append(f"{env['canvas']}-{task['id']}-logs")
    save(env["parser_record_path"], env["parser_record"])
    with Session(env["engine"]) as db:
        task.update(task_type="dataflow", dataflow_id=env["canvas"], file=None)
        asyncio.run(worker.run_dataflow(db, task))
    assert any(item.get("path") == ["File", "Parser", "TokenChunker", "Tokenizer"] for item in seen)
    with Session(env["engine"]) as db:
        doc = db.get(Document, env["docs"]["a"])
        assert doc.token_num == 7 and doc.chunk_num > 0 and db.get(Task, task["id"]).progress == 1
    assert patch(env, {"chunk_method": "naive"})["data"]["pipeline_id"] == ""

    def model(*args: Any, **kwargs: Any) -> Any:
        def encode(texts: list[str]) -> tuple[Any, int]:
            seen.append({"builtin_embedding_input": texts})
            return np.array([[0.2] * 768 for _ in texts]), 3

        return SimpleNamespace(encode=encode, max_length=8192, llm_name="controlled-local-embedding")

    monkeypatch.setattr(worker, "get_model_config_by_type_and_name", lambda *args, **kwargs: {})
    monkeypatch.setattr(worker, "LLMBundle", model)
    task = submit()
    with Session(env["engine"]) as db:
        task.update(task_type="")
        asyncio.run(worker.do_handle_task(db, task))
    assert any("Original source a" in str(item.get("builtin_embedding_input", "")) for item in seen)
    with Session(env["engine"]) as db:
        doc = db.get(Document, env["docs"]["a"])
        assert doc.pipeline_id == "" and doc.parser_id == "naive" and doc.chunk_num > 0
        assert db.get(Task, task["id"]).progress == 1
    env["parser_record"]["real_component_source_chain"] = {"stages": seen, "stores": snapshot(env)}
    save(env["parser_record_path"], env["parser_record"])


def test_actual_api_key_jwt_disable_sdk_and_missing_paths(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.db_models import APIToken, User

    env = parser_api
    assert patch(env, {"parser_config": {}}, role="api", key="foreign", dataset=env["ids"]["foreign"])["data"]["enabled"] is True
    before = snapshot(env)
    with monkeypatch.context() as disabled:
        disabled.setenv("DISABLE_SDK", "1")
        patch(env, {}, role="api", key="foreign", dataset=env["ids"]["foreign"], status=401)
        assert snapshot(env) == before
        assert patch(env, {}, role="owner")["code"] == 0
    with Session(env["engine"]) as db:
        db.execute(sa.update(User).where(User.id == env["ids"]["owner"]).values(access_token="INVALID_controlled"))
        db.add(APIToken(tenant_id=env["ids"]["other"], token=env["tokens"]["owner"], name="image-http"))
        db.commit()
    before = snapshot(env)
    patch(env, {}, role="owner", status=401)
    assert snapshot(env) == before
    patch(env, {}, role="admin", dataset=env["ids"]["foreign"], status=404)
    patch(env, {}, role="api", key="foreign", dataset=uuid4().hex, status=404)
    assert snapshot(env) == before


def test_real_source_metadata_and_all_config_families_preserve_historical_fields(parser_api: dict[str, Any]) -> None:
    env = parser_api
    for kind, suffix, parser in [("visual", "png", "picture"), ("aural", "wav", "audio"), ("doc", "pptx", "presentation"), ("doc", "eml", "email")]:
        key = "actual-source." + suffix
        manifest = env["parser_record"]["manifest"]
        manifest["objects"].append((env["ids"]["kb"], key))
        save(env["parser_record_path"], env["parser_record"])
        env["storage"].put(env["ids"]["kb"], key, ("Controlled saved source " + suffix).encode())
        with Session(env["engine"]) as db:
            db.execute(sa.update(Document).where(Document.id == env["docs"]["a"]).values(type=kind, parser_id=parser))
            db.execute(sa.update(File).where(File.id == manifest["files"][0]).values(location=key))
            db.commit()
        before = snapshot(env)
        patch(env, {"name": "renamed-display.txt", "chunk_method": "naive", "enabled": True, "meta_fields": {"score": 1}}, status=400)
        assert snapshot(env) == before
        assert patch(env, {"chunk_method": parser})["data"]["pipeline_id"] is None
    config = {
        "chunk_token_num": 8192,
        "delimiter": ";",
        "pages": [[1, 2]],
        "html4excel": False,
        "toc_extraction": False,
        "filename_embd_weight": 0.2,
        "overlapped_percent": 0.1,
        "tag_kb_ids": [],
        "mineru_lang": "English",
        "mineru_parse_method": "auto",
        "raptor": {"use_raptor": False},
        "graphrag": {"use_graphrag": False, "entity_types": []},
        "parent_child": {"use_parent_child": True, "children_delimiter": ";"},
        "metadata": {"properties": {"year": {"type": "integer"}}},
        "enable_metadata": True,
        "built_in_metadata": [],
    }
    before = snapshot(env)
    data = patch(env, {"parser_config": config})["data"]
    assert data["parser_config"]["unknown"] == [0] and data["parser_config"]["raptor"]["future"] is None and data["parser_config"]["llm_id"] == "preserved"
    assert data["pipeline_id"] is None and data["state"] == "DONE"
    after = snapshot(env)
    assert after["index"] == before["index"] and after["objects"] == before["objects"] and after["queue"] == before["queue"]
    assert after["sql"][Task.__tablename__] == before["sql"][Task.__tablename__]


def test_c810_reset_revokes_all_late_old_generation_writes(parser_api: dict[str, Any]) -> None:
    from api.db.services.document_status_service import insert_source_chunks
    from api.db.services.document_task_service import SupersededDocumentTask, cleanup_task_chunks, increment_task_document, put_task_image, write_task_metadata

    env = parser_api
    old_id = env["parser_record"]["manifest"]["tasks"][0]
    assert patch(env, {"chunk_method": "paper"})["code"] == 0
    before = snapshot(env)
    row = copy.deepcopy(env["parser_record"]["source_rows"][1])
    operations = [
        lambda: insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], old_id),
        lambda: increment_task_document(env["engine"], old_id, env["docs"]["a"], env["ids"]["kb"], 99, 1, 0),
        lambda: put_task_image(old_id, env["docs"]["a"], env["ids"]["kb"], env["ids"]["owner"], bucket=env["ids"]["kb"], fnm="history.png", binary=b"Late old image"),
        lambda: write_task_metadata(env["engine"], old_id, env["docs"]["a"], env["ids"]["kb"], {"stale": True}),
    ]
    for operation in operations:
        with pytest.raises(SupersededDocumentTask):
            operation()
        assert snapshot(env) == before
    cleanup_task_chunks(env["engine"], old_id, env["docs"]["a"], env["ids"]["kb"], env["collections"]["kb"], [row["id"]])
    assert snapshot(env) == before


def test_committed_unknown_recovery_preserves_later_same_value_fields_and_other_doc_ledger(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from api.db.services.document_status_service import insert_source_chunks
    from api.db.services.document_task_service import increment_task_document, put_task_image

    env = parser_api
    assert patch(env, {"enabled": True})["data"]["enabled"] is True
    ready, release = threading.Event(), threading.Event()
    once = False

    def before_commit(db: Session) -> None:
        nonlocal once
        if db.bind is env["engine"] and not once and db.scalar(sa.select(Document.name).where(Document.id == env["docs"]["a"])) == "committed.txt":
            db.info["c810_lost_commit_response"] = True
            once = True

    def after_commit(db: Session) -> None:
        if db.info.pop("c810_lost_commit_response", False):
            ready.set()
            assert release.wait(20)
            raise RuntimeError("Controlled lost COMMIT response")

    sa.event.listen(Session, "before_commit", before_commit)
    sa.event.listen(Session, "after_commit", after_commit)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            failed = pool.submit(patch, env, {"chunk_method": "paper", "name": "committed.txt", "enabled": False, "meta_fields": {"score": 1}}, status=500)
            try:
                assert ready.wait(10)
                status = requests.post(
                    env["base"] + f"/api/v1/datasets/{env['ids']['kb']}/documents/batch-update-status",
                    json={"doc_ids": [env["docs"]["a"]], "status": 0},
                    headers={"Authorization": "Bearer " + env["tokens"]["owner"]},
                    timeout=10,
                )
                assert status.status_code == 200 and status.json()["code"] == 0
                # These controlled independent writers intentionally assign the
                # exact same values. PostgreSQL xmin distinguishes ownership.
                with Session(env["engine"]) as db:
                    db.execute(sa.update(File).where(File.id == env["parser_record"]["manifest"]["files"][0]).values(name="committed.txt"))
                    db.execute(sa.update(DocumentMetadata).where(DocumentMetadata.id == env["docs"]["a"]).values(meta_fields={"score": 1}))
                    db.commit()
                response = requests.post(
                    env["base"] + "/api/v1/documents/ingest", json={"doc_ids": [env["docs"]["b"]], "run": 1}, headers={"Authorization": "Bearer " + env["tokens"]["owner"]}, timeout=10
                )
                assert response.json()["code"] == 0
                with Session(env["engine"]) as db:
                    task = db.scalar(sa.select(Task).where(Task.doc_id == env["docs"]["b"]))
                    task_id = task.id
                later_image = pool.submit(
                    put_task_image, task_id, env["docs"]["b"], env["ids"]["kb"], env["ids"]["owner"], bucket=env["ids"]["kb"], fnm="history.png", binary=b"Later generation image bytes"
                )
                # This exact key is serialized until the first writer drains.
                # Its durable unknown outcome still precedes the later winner
                # and the explicit recovery retry.
                release.set()
                assert failed.result(10)["code"] == "DOCUMENT_UPDATE_OUTCOME_UNKNOWN"
                later_image.result(10)
                row = copy.deepcopy(env["parser_record"]["source_rows"][3])
                row.update(id=uuid4().hex, doc_id=env["docs"]["b"], img_id=env["ids"]["kb"] + "-history.png")
                row["pk"] = row["id"]
                insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id)
                increment_task_document(env["engine"], task_id, env["docs"]["b"], env["ids"]["kb"], 5, 1, 0)
                later = snapshot(env)
                env["parser_record"]["later_winners_before_recovery"] = later
                save(env["parser_record_path"], env["parser_record"])
            finally:
                release.set()
            assert failed.result(10)["code"] == "DOCUMENT_UPDATE_OUTCOME_UNKNOWN"
    finally:
        sa.event.remove(Session, "before_commit", before_commit)
        sa.event.remove(Session, "after_commit", after_commit)
    assert REDIS_CONN.REDIS.exists(recovery_key(env["docs"]["a"]))
    assert_durable_journal(env)
    data = patch(env, {"parser_config": {}})["data"]
    assert data["name"] == "a.txt" and data["chunk_method"] == "naive" and data["enabled"] is False and data["chunk_count"] == 2
    assert data["meta_fields"] == {"score": 1}
    assert env["storage"].get_bytes(env["ids"]["kb"], "history.png") == b"Later generation image bytes"
    after = snapshot(env)
    with Session(env["engine"]) as db:
        assert db.get(File, env["parser_record"]["manifest"]["files"][0]).name == "committed.txt"
        kb = db.get(Knowledgebase, env["ids"]["kb"])
        assert (kb.chunk_num, kb.token_num) == (4, 5)
    for model, field in [(Document, "id"), (Task, "doc_id")]:
        expected = [row for row in later["sql"][model.__tablename__] if row[field] == env["docs"]["b"]]
        assert [row for row in after["sql"][model.__tablename__] if row[field] == env["docs"]["b"]] == expected
    assert [row for row in after["index"]["kb"]["rows"] if row["doc_id"] == env["docs"]["b"]] == [row for row in later["index"]["kb"]["rows"] if row["doc_id"] == env["docs"]["b"]]
    assert after["queue"] == later["queue"] and not REDIS_CONN.REDIS.exists(recovery_key(env["docs"]["a"]))


def test_encrypted_image_restore_preserves_native_ciphertext(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from core.utils.encrypted_storage import EncryptedStorageWrapper

    env = parser_api
    storage = EncryptedStorageWrapper(env["adapter"], key="scratch-parser-key")
    plaintext = b"Exact encrypted history image"
    storage.put(env["ids"]["kb"], "history.png", plaintext)
    ciphertext = env["adapter"].get_bytes(env["ids"]["kb"], "history.png")
    assert ciphertext is not None and ciphertext != plaintext
    before = snapshot(env)

    def late_sql(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Controlled SQL failure after encrypted image deletion")

    with monkeypatch.context() as failure:
        failure.setitem(resources._state, "storage", storage)
        failure.setattr(ingest_service, "_set_document", late_sql)
        assert patch(env, {"chunk_method": "paper"}, status=500)["details"]["outcome"] == "unchanged"
        assert storage.get_bytes(env["ids"]["kb"], "history.png") == plaintext
        assert env["adapter"].get_bytes(env["ids"]["kb"], "history.png") == ciphertext
    assert snapshot(env) == before
    env["parser_record"]["encrypted_restore"] = {"plaintext": plaintext, "ciphertext": ciphertext, "stores": snapshot(env)}
    save(env["parser_record_path"], env["parser_record"])
    with monkeypatch.context() as encrypted:
        encrypted.setitem(resources._state, "storage", storage)
        assert patch(env, {"chunk_method": "paper"})["data"]["chunk_count"] == 0
        assert env["adapter"].get_bytes(env["ids"]["kb"], "history.png") is None


def test_missing_native_table_and_failed_availability_ack_are_safe(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = parser_api
    before = snapshot(env)
    original = settings.docStoreConn.update
    once = False

    def update(*args: Any, **kwargs: Any) -> bool:
        nonlocal once
        result = original(*args, **kwargs)
        if not once:
            once = True
            return False
        return result

    with monkeypatch.context() as fault:
        fault.setattr(settings.docStoreConn, "update", update)
        assert patch(env, {"enabled": True}, status=500)["details"]["outcome"] == "unchanged"
    assert snapshot(env) == before
    settings.docStoreConn.delete_idx(env["collections"]["kb"], env["ids"]["kb"])
    missing = snapshot(env)
    assert patch(env, {"chunk_method": "paper", "meta_fields": {"score": 1}, "name": "new.txt"}, status=500)["details"]["outcome"] == "unchanged"
    assert snapshot(env) == missing


@pytest.mark.parametrize("stage", ["initial_sql", "unexpected_save"])
def test_canonical_generic_failure_is_safe_and_keeps_outcome(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    env = parser_api
    before = snapshot(env)
    full_before = full_readback(env)

    def sql_failure(connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        if "FROM usr_ai.t_ai_documents" in statement:
            raise RuntimeError("private SQL bind secret")

    async def save_failure(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("private SQL bind secret")

    if stage == "initial_sql":
        sa.event.listen(env["async_engine"].sync_engine, "before_cursor_execute", sql_failure)
    else:
        monkeypatch.setattr(sys.modules["api.apps.restful_apis.document"], "update_document_parser", save_failure)
    try:
        # The injected SELECT only affects the mutation; independent readback is
        # performed after removing the failure event below.
        response = requests.patch(
            env["base"] + f"/api/v1/datasets/{env['ids']['kb']}/documents/{env['docs']['a']}",
            json={"chunk_method": "paper"},
            headers={"Authorization": "Bearer " + env["tokens"]["owner"]},
            timeout=30,
            allow_redirects=False,
        )
    finally:
        if stage == "initial_sql":
            sa.event.remove(env["async_engine"].sync_engine, "before_cursor_execute", sql_failure)
    readback = requests.get(env["base"] + f"/api/v1/datasets/{env['ids']['kb']}/documents", params={"id": env["docs"]["a"]}, headers={"Authorization": "Bearer " + env["tokens"]["owner"]}, timeout=30)
    assert readback.status_code == 200 and readback.json()["code"] == 0 and len(readback.json()["data"]["docs"]) == 1
    body = response.json()
    assert response.status_code == 500 and body["retcode"] == 500 and "private" not in response.text and "secret" not in response.text
    assert body["code"] == ("DOCUMENT_UPDATE_FAILED" if stage == "initial_sql" else "DOCUMENT_UPDATE_OUTCOME_UNKNOWN")
    assert body["details"] == body["data"] == {"outcome": "unchanged" if stage == "initial_sql" else "unknown"}
    assert body["detail"] == body["message"] == body["retmsg"] and body["request_id"] == response.headers["X-Request-ID"]
    assert snapshot(env) == before
    env["parser_record"]["events"].append(
        {
            "path": response.request.path_url,
            "status": response.status_code,
            "body": body,
            "headers": dict(response.headers),
            "raw": response.content,
            "stage": stage,
            "stores": snapshot(env),
            "full_stores_before": full_before,
            "full_stores_after": full_readback(env),
            "authenticated_list_readback": {"status": readback.status_code, "body": readback.json(), "headers": dict(readback.headers), "raw": readback.content},
        }
    )
    save(env["parser_record_path"], env["parser_record"])


def test_foreign_native_orphan_reference_protects_history_image(parser_api: dict[str, Any]) -> None:
    env = parser_api
    row = copy.deepcopy(env["parser_record"]["source_rows"][4])
    row.update(id=uuid4().hex, doc_id=uuid4().hex, img_id=env["ids"]["kb"] + "-history.png")
    row["pk"] = row["id"]
    env["parser_record"]["manifest"]["native_orphan"] = {"collection": env["collections"]["foreign"], "row_id": row["id"], "document_id": row["doc_id"]}
    save(env["parser_record_path"], env["parser_record"])
    with Session(env["engine"]) as db:
        assert db.get(Document, row["doc_id"]) is None
    assert settings.docStoreConn.insert([row], env["collections"]["foreign"], env["ids"]["foreign"]) == []
    before = snapshot(env)
    data = patch(env, {"chunk_method": "paper"})["data"]
    assert data["chunk_count"] == 0 and data["state"] == "UNSTART"
    after = snapshot(env)
    assert after["index"]["foreign"] == before["index"]["foreign"] and after["objects"] == before["objects"]
    assert env["storage"].get_bytes(env["ids"]["kb"], "history.png") is not None


@pytest.mark.parametrize("source", ["gmail", "knowledgebase"])
def test_nonlocal_actual_source_address_controls_parser(parser_api: dict[str, Any], source: str) -> None:
    from api.db.services.file2document_service import File2DocumentService

    env = parser_api
    location = "actual-source.eml"
    content = b"From: sender@example.test\nTo: receiver@example.test\nSubject: Original mail\n\nStored email source."
    env["storage"].put(env["ids"]["kb"], location, content)
    env["storage"].put(env["ids"]["kb"], "origin.pdf", b"Original nonlocal catalog file")
    with Session(env["engine"]) as db:
        db.execute(sa.update(File).where(File.id == env["parser_record"]["manifest"]["files"][0]).values(source_type=source, location="origin.pdf", name="linked.pdf"))
        db.execute(sa.update(Document).where(Document.id == env["docs"]["a"]).values(source_type=source, location=location))
        db.commit()
        address = File2DocumentService.get_storage_address(db, doc_id=env["docs"]["a"])
    assert address == (env["ids"]["kb"], location) and env["storage"].get_bytes(*address) == content
    before = snapshot(env)
    assert patch(env, {"chunk_method": "naive", "name": "display.txt", "enabled": True, "meta_fields": {"score": 1}}, status=400)["details"]["outcome"] == "unchanged"
    assert snapshot(env) == before
    assert patch(env, {"chunk_method": "email", "name": "display.txt"})["data"]["chunk_method"] == "email"
    assert env["storage"].get_bytes(*address) == content
    env["parser_record"]["nonlocal_actual_source"] = {"source_type": source, "address": address, "bytes": content, "stores": snapshot(env)}
    save(env["parser_record_path"], env["parser_record"])


@pytest.mark.parametrize("window", ["delete", "restore"])
@pytest.mark.parametrize("first", ["image_write", "reference_insert"])
def test_actual_exact_image_lock_serializes_mutation_windows(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, window: str, first: str) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from api.db.services.document_status_service import insert_source_chunks
    from api.db.services.document_task_service import put_task_image

    env = parser_api
    response = requests.post(env["base"] + "/api/v1/documents/ingest", json={"doc_ids": [env["docs"]["b"]], "run": 1}, headers={"Authorization": "Bearer " + env["tokens"]["owner"]}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0
    with Session(env["engine"]) as db:
        task_id = db.scalar(sa.select(Task.id).where(Task.doc_id == env["docs"]["b"]))
    before = snapshot(env)
    row = copy.deepcopy(env["parser_record"]["source_rows"][3])
    row.update(id=uuid4().hex, doc_id=env["docs"]["b"], img_id=env["ids"]["kb"] + "-history.png")
    row["pk"] = row["id"]
    env["parser_record"]["manifest"]["new_native_chunk_id"] = row["id"]
    save(env["parser_record_path"], env["parser_record"])
    original_raw = env["adapter"].get_bytes(env["ids"]["kb"], "history.png")
    ready, release, attempted = threading.Event(), threading.Event(), threading.Event()
    writer_thread: list[int] = []
    key = int.from_bytes(hashlib.sha256(("document-image\0" + env["ids"]["kb"] + "\0history.png").encode()).digest()[:8], signed=True)
    original_rm, original_put = env["storage"].rm, env["adapter"].put

    def pause() -> None:
        ready.set()
        assert release.wait(20)

    def rm(*args: Any, **kwargs: Any) -> Any:
        if args[:2] == (env["ids"]["kb"], "history.png"):
            pause()
        return original_rm(*args, **kwargs)

    def put(*args: Any, **kwargs: Any) -> Any:
        namespace = args[0] if args else kwargs["bucket"]
        name = args[1] if len(args) > 1 else kwargs["fnm"]
        binary = args[2] if len(args) > 2 else kwargs["binary"]
        if (namespace, name, binary) == (env["ids"]["kb"], "history.png", original_raw):
            pause()
        return original_put(*args, **kwargs)

    def late_sql(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Controlled failure before image compensation")

    def lock_attempt(connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        if writer_thread and threading.get_ident() == writer_thread[0] and "pg_advisory_xact_lock" in statement and parameters["key"] == key:
            attempted.set()

    winner = b"Later writer bytes inside the image mutation window"

    def writer() -> None:
        writer_thread.append(threading.get_ident())

        def image() -> None:
            put_task_image(task_id, env["docs"]["b"], env["ids"]["kb"], env["ids"]["owner"], bucket=env["ids"]["kb"], fnm="history.png", binary=winner)

        def reference() -> None:
            insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id)

        if first == "image_write":
            image()
            reference()
        else:
            reference()
            image()

    sa.event.listen(env["engine"], "before_cursor_execute", lock_attempt)
    try:
        with monkeypatch.context() as fault:
            if window == "delete":
                fault.setattr(env["storage"], "rm", rm)
            else:
                fault.setattr(ingest_service, "_set_document", late_sql)
                fault.setattr(env["adapter"], "put", put)
            with ThreadPoolExecutor(max_workers=2) as pool:
                mutation = pool.submit(patch, env, {"chunk_method": "paper"}, status=200 if window == "delete" else 500)
                try:
                    assert ready.wait(10)
                    later = pool.submit(writer)
                    assert attempted.wait(10)
                    unsigned = key & ((1 << 64) - 1)
                    deadline = time.monotonic() + 4
                    while True:
                        with env["engine"].connect() as db:
                            locks = [
                                dict(r)
                                for r in db.execute(
                                    sa.text(
                                        "SELECT pid, granted FROM pg_locks WHERE locktype='advisory' AND classid=:high AND objid=:low AND objsubid=1 AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"
                                    ),
                                    {"high": unsigned >> 32, "low": unsigned & ((1 << 32) - 1)},
                                ).mappings()
                            ]
                        if any(not lock["granted"] for lock in locks):
                            break
                        assert time.monotonic() < deadline
                        time.sleep(0.02)
                    assert not later.done()
                    env["parser_record"]["blocked_mutation_window"] = {"window": window, "first": first, "sql_locks": locks, "stores": snapshot(env)}
                    save(env["parser_record_path"], env["parser_record"])
                finally:
                    release.set()
                body = mutation.result(30)
                later.result(30)
                if window == "restore":
                    assert body["code"] == "DOCUMENT_UPDATE_FAILED" and body["details"]["outcome"] == "unchanged"
    finally:
        release.set()
        sa.event.remove(env["engine"], "before_cursor_execute", lock_attempt)
    assert env["storage"].get_bytes(env["ids"]["kb"], "history.png") == winner
    after = snapshot(env)
    native = next(item for item in after["index"]["kb"]["rows"] if item["id"] == row["id"])
    assert native["img_id"] == row["img_id"] and native["q_768_vec"] and native["vector"]
    assert after["index"]["foreign"] == before["index"]["foreign"] and after["queue"] == before["queue"]
    with Session(env["engine"]) as db:
        assert db.get(Document, env["docs"]["b"]).chunk_num == 2
        assert db.get(Knowledgebase, env["ids"]["kb"]).chunk_num == (2 if window == "delete" else 4)
    assert not REDIS_CONN.REDIS.exists(recovery_key(env["docs"]["a"]))
    env["parser_record"]["mutation_window_winner"] = {"bytes": winner, "native_row": native, "stores": after}
    save(env["parser_record_path"], env["parser_record"])


@pytest.mark.parametrize("completion", ["insert", "cancel"])
def test_current_task_pending_image_survives_put_to_reference_gap(parser_api: dict[str, Any], completion: str) -> None:
    from concurrent.futures import ThreadPoolExecutor

    env = parser_api
    response = requests.post(env["base"] + "/api/v1/documents/ingest", json={"doc_ids": [env["docs"]["b"]], "run": 1}, headers={"Authorization": "Bearer " + env["tokens"]["owner"]}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0
    with Session(env["engine"]) as db:
        task_id = db.scalar(sa.select(Task.id).where(Task.doc_id == env["docs"]["b"], Task.progress < 1, Task.progress >= 0))
    assert task_id is not None
    row = copy.deepcopy(env["parser_record"]["source_rows"][3])
    row.update(id=uuid4().hex, img_id=env["ids"]["kb"] + "-history.png")
    row["pk"] = row["id"]
    winner = b"Current producer bytes, put returned before native reference"
    ready, release = threading.Event(), threading.Event()

    def producer() -> str:
        task_service.put_task_image(task_id, env["docs"]["b"], env["ids"]["kb"], env["ids"]["owner"], bucket=env["ids"]["kb"], fnm="history.png", binary=winner)
        ready.set()
        assert release.wait(20)
        try:
            assert status_service.insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id) == []
            return "inserted"
        except task_service.SupersededDocumentTask:
            assert completion == "cancel"
            return "superseded"

    before = snapshot(env)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(producer)
        try:
            assert ready.wait(10)
            pending = snapshot(env)
            assert pending["sql"] == before["sql"] and pending["index"] == before["index"] and pending["queue"] == before["queue"]
            assert REDIS_CONN.REDIS.sismember(image_lock.task_image_reservation_key(task_id), json.dumps([env["ids"]["kb"], "history.png"], separators=(",", ":")))
            env["parser_record"]["after_put_before_reference"] = pending
            save(env["parser_record_path"], env["parser_record"])
            if completion == "cancel":
                canceled = requests.post(
                    env["base"] + "/api/v1/documents/ingest", json={"doc_ids": [env["docs"]["b"]], "run": 2}, headers={"Authorization": "Bearer " + env["tokens"]["owner"]}, timeout=30
                )
                assert canceled.status_code == 200 and canceled.json()["code"] == 0
                assert not REDIS_CONN.REDIS.exists(image_lock.task_image_reservation_key(task_id))
            patch(env, {"chunk_method": "paper"})
            middle = snapshot(env)
            assert env["storage"].get_bytes(env["ids"]["kb"], "history.png") == (winner if completion == "insert" else None)
            assert middle["index"]["foreign"] == before["index"]["foreign"] and middle["queue"] == before["queue"]
            env["parser_record"]["reset_before_reference"] = middle
            save(env["parser_record_path"], env["parser_record"])
        finally:
            release.set()
        assert future.result(20) == ("inserted" if completion == "insert" else "superseded")
    after = snapshot(env)
    native = [item for item in after["index"]["kb"]["rows"] if item["id"] == row["id"]]
    if completion == "insert":
        assert len(native) == 1 and native[0]["img_id"] == row["img_id"]
        assert native[0]["vector"] == before["index"]["kb"]["rows"][0]["vector"] and native[0]["q_768_vec"]
        assert env["storage"].get_bytes(env["ids"]["kb"], "history.png") == winner
    else:
        assert not native
    with Session(env["engine"]) as db:
        doc = db.get(Document, env["docs"]["b"])
        task = db.get(Task, task_id)
        assert doc.chunk_num == (2 if completion == "insert" else 1)
        assert db.get(Knowledgebase, env["ids"]["kb"]).chunk_num == doc.chunk_num
        assert task.chunk_ids == (row["id"] if completion == "insert" else "")
        assert task.progress >= 0 if completion == "insert" else task.progress < 0
    assert after["queue"] == before["queue"] and not REDIS_CONN.REDIS.exists(image_lock.task_image_reservation_key(task_id))
    env["parser_record"]["completed_pending_reference"] = {"completion": completion, "native": native, "stores": after}
    save(env["parser_record_path"], env["parser_record"])


def test_source_rollback_recovery_releases_image_before_relocking_document(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    env = parser_api
    response = requests.post(env["base"] + "/api/v1/documents/ingest", json={"doc_ids": [env["docs"]["b"]], "run": 1}, headers={"Authorization": "Bearer " + env["tokens"]["owner"]}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0
    with Session(env["engine"]) as db:
        task_id = db.scalar(sa.select(Task.id).where(Task.doc_id == env["docs"]["b"], Task.progress < 1, Task.progress >= 0))
    assert task_id is not None
    row = copy.deepcopy(env["parser_record"]["source_rows"][3])
    row.update(id=uuid4().hex, img_id=env["ids"]["kb"] + "-history.png")
    row["pk"] = row["id"]
    before = snapshot(env)
    rolled_back, winner_locked, recover_attempted = threading.Event(), threading.Event(), threading.Event()
    source_thread: list[int] = []
    original_insert, original_rollback, original_owner = settings.docStoreConn.insert, Session.rollback, task_service.current_document_task
    winner_bytes = b"Same current task image winner after source rollback"

    def failing_insert(chunks: list[dict[str, Any]], *args: Any, **kwargs: Any) -> list[str]:
        result = original_insert(chunks, *args, **kwargs)
        if threading.get_ident() == source_thread[0] and any(chunk.get("id") == row["id"] for chunk in chunks):
            assert result == []
            raise RuntimeError("Controlled source insert failure before SQL flush")
        return result

    def rollback(db: Session) -> None:
        original_rollback(db)
        if source_thread and threading.get_ident() == source_thread[0] and not rolled_back.is_set():
            rolled_back.set()
            assert winner_locked.wait(10)

    from contextlib import contextmanager

    @contextmanager
    def owner(db: Session, *args: Any, **kwargs: Any) -> Iterator[Any]:
        if source_thread and threading.get_ident() == source_thread[0] and kwargs.get("cleanup"):
            recover_attempted.set()
        with original_owner(db, *args, **kwargs) as task:
            if source_thread and threading.get_ident() != source_thread[0]:
                winner_locked.set()
                assert recover_attempted.wait(10)
            yield task

    def source() -> str:
        source_thread.append(threading.get_ident())
        try:
            status_service.insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id)
        except RuntimeError as error:
            assert str(error) == "Controlled source insert failure before SQL flush"
            return "failed-and-restored"
        raise AssertionError("Failed source insert reported success")

    def winner() -> None:
        assert rolled_back.wait(10)
        task_service.put_task_image(task_id, env["docs"]["b"], env["ids"]["kb"], env["ids"]["owner"], bucket=env["ids"]["kb"], fnm="history.png", binary=winner_bytes)

    def bounded_wait(connection: sa.Connection) -> None:
        # A future lock-order regression must fail the assertions and drain
        # both real transactions, rather than leave daemon threads behind.
        connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '8s'")

    sa.event.listen(env["engine"], "begin", bounded_wait)
    try:
        with monkeypatch.context() as fault:
            fault.setattr(settings.docStoreConn, "insert", failing_insert)
            fault.setattr(Session, "rollback", rollback)
            fault.setattr(task_service, "current_document_task", owner)
            fault.setattr(status_service, "current_document_task", owner)
            with ThreadPoolExecutor(max_workers=2) as pool:
                insertion, writer = pool.submit(source), pool.submit(winner)
                assert insertion.result(20) == "failed-and-restored"
                writer.result(20)
    finally:
        sa.event.remove(env["engine"], "begin", bounded_wait)
    assert rolled_back.is_set() and winner_locked.is_set() and recover_attempted.is_set()
    after = snapshot(env)
    assert after["sql"] == before["sql"] and after["index"] == before["index"] and after["queue"] == before["queue"]
    assert env["storage"].get_bytes(env["ids"]["kb"], "history.png") == winner_bytes
    env["parser_record"]["source_failure_same_task_winner"] = {"outcome": "failed-and-restored", "stores": after, "bytes": winner_bytes}
    save(env["parser_record_path"], env["parser_record"])
    # The failed attempt retains its trusted pending reference for a retry.
    assert status_service.insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id) == []
    final = snapshot(env)
    native = next(item for item in final["index"]["kb"]["rows"] if item["id"] == row["id"])
    assert native["img_id"] == row["img_id"] and native["vector"] and native["q_768_vec"]
    assert env["storage"].get_bytes(env["ids"]["kb"], "history.png") == winner_bytes
    assert not REDIS_CONN.REDIS.exists(image_lock.task_image_reservation_key(task_id))
    with Session(env["engine"]) as db:
        assert db.get(Document, env["docs"]["b"]).chunk_num == 2 and db.get(Knowledgebase, env["ids"]["kb"]).chunk_num == 4
        assert db.get(Task, task_id).chunk_ids == row["id"]
    env["parser_record"]["source_failure_retry"] = final
    save(env["parser_record_path"], env["parser_record"])
