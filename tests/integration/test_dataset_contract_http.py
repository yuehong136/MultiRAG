"""Dataset contracts over a real listener, auth, PostgreSQL and owned storage."""

import copy
import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.services import dataset_api_service
from api.db import FileType
from api.db.db_models import LLM, Document, DocumentMetadata, File, File2Document, Knowledgebase, LLMFactories, Tenant, TenantLLM
from api.db.services import document_ingest_service, knowledgebase_service
from common import settings
from common.constants import FileSource
from tests.support.database import scratch_database
from tests.support.dataset_contract_cases import CREATE_INVALID_PARSER_CASES, CREATE_VALID_PARSER_CASES, UPDATE_INVALID_PARSER_CASES, UPDATE_VALID_PARSER_CASES
from tests.support.dataset_management_http import management_api as management_api
from tests.support.dataset_management_http import request_api, sql_state


@pytest.fixture(scope="session")
def pg_scratch_engine() -> Iterator[sa.Engine]:
    evidence = Path(os.environ.get("MULTIRAG_DATASET_CONTRACT_EVIDENCE", ".test-results/dataset-contract"))
    with scratch_database(evidence) as engine:
        yield engine


@pytest.fixture
def contracts(management_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = management_api
    record_path = env["record_path"].with_suffix(".contracts.json")
    record: dict[str, Any] = {"datasets": env["datasets"], "files": [], "links": [], "models": [], "database": env["engine"].url.database}

    def save() -> None:
        record_path.write_text(json.dumps(record))

    def identifier() -> str:
        value = uuid4().hex
        env["datasets"].append(value)
        save()  # Before the production insert, including denied/failed creates.
        return value

    @contextmanager
    def db_connection() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db
            db.commit()

    monkeypatch.setattr(dataset_api_service, "db_connection", db_connection)
    monkeypatch.setattr(document_ingest_service, "db_connection", db_connection)
    monkeypatch.setattr(knowledgebase_service, "get_uuid", identifier)
    env["contract_record"] = record
    env["save_record"] = save
    save()
    try:
        yield env
    finally:
        with env["engine"].begin() as db:
            for model, ids in [(DocumentMetadata, env["documents"]), (File2Document, record["links"]), (File, record["files"]), (TenantLLM, record["models"])]:
                db.execute(sa.delete(model).where(model.id.in_(ids)))
        with env["engine"].connect() as db:
            remaining = {
                model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model).where(model.id.in_(ids)))
                for model, ids in [(DocumentMetadata, env["documents"]), (File2Document, record["links"]), (File, record["files"]), (TenantLLM, record["models"])]
            }
        assert not any(remaining.values()), remaining
        record["remaining"] = remaining
        from core.utils.redis_conn import REDIS_CONN

        for key in record.get("redis", []):
            REDIS_CONN.REDIS.delete(key)
            assert not REDIS_CONN.REDIS.exists(key)
        record["redis_absent"] = True
        save()


def api(env: dict[str, Any], method: str, suffix: str, payload: Any = None, params: Any = None) -> dict[str, Any]:
    response = request_api(env, method, suffix, credential=env["keys"][0], payload=payload, params=params)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["code"] == 0, result
    return result


def create(env: dict[str, Any], **fields: Any) -> dict[str, Any]:
    fields.setdefault("name", "contract_" + uuid4().hex)
    return api(env, "POST", "/datasets", fields)["data"]


def stored(env: dict[str, Any], identifier: str) -> dict[str, Any]:
    return sql_state(env)[Knowledgebase.__tablename__][identifier]


def test_create_avatar_defaults_and_duplicate_http_sql(contracts: dict[str, Any]) -> None:
    env = contracts
    for avatar in [None, "", "data:image/png;base64,aGVsbG8="]:
        data = create(env, avatar=avatar, embedding_model=None, description="")
        row = stored(env, data["id"])
        assert row["avatar"] == data["avatar"] == avatar
        assert row["description"] == data["description"] == ""
        assert row["parser_id"] == data["chunk_method"] == "naive"
        assert data["parser_config"]["raptor"]["use_raptor"] is True  # Current local default.
        before = sql_state(env)
        denied = request_api(env, "POST", "/datasets", credential=env["keys"][0], payload={"name": data["name"]})
        assert denied.status_code == 200 and denied.json() == {"code": 102, "message": f"Dataset name '{data['name']}' already exists"}
        assert sql_state(env) == before


def test_create_parser_methods_and_valid_matrices_http_sql(contracts: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = contracts
    monkeypatch.setenv("DOC_ENGINE", "elasticsearch")  # No store I/O in creation; enables the tag parser contract.
    monkeypatch.setattr(settings, "DOC_ENGINE_INFINITY", False)
    update_target = create(env)
    for method in ["naive", "book", "email", "laws", "manual", "one", "paper", "picture", "presentation", "qa", "table", "tag", "resume"]:
        data = create(env, chunk_method=method)
        assert stored(env, data["id"])["parser_id"] == data["chunk_method"] == method
        updated = api(env, "PUT", "/datasets/" + update_target["id"], {"chunk_method": method})["data"]
        assert stored(env, update_target["id"])["parser_id"] == updated["chunk_method"] == method
    for name, config in CREATE_VALID_PARSER_CASES:
        data = create(env, name="matrix_" + name, parser_config=config)
        row = stored(env, data["id"])
        assert row["parser_config"] == data["parser_config"]
        for key, value in config.items():
            if key == "parent_child":
                assert row["parser_config"]["enable_children"] == value.get("use_parent_child", False)
                if "children_delimiter" in value:
                    assert row["parser_config"]["children_delimiter"] == (value["children_delimiter"] if value.get("use_parent_child", False) else "")
            elif isinstance(value, dict):
                assert all(row["parser_config"][key][k] == v for k, v in value.items())
            else:
                assert row["parser_config"][key] == value


def test_invalid_create_and_update_have_zero_sql_effect(contracts: dict[str, Any]) -> None:
    env = contracts
    data = create(env)
    for method, path, cases in [
        ("POST", "/datasets", CREATE_INVALID_PARSER_CASES),
        ("PUT", "/datasets/" + data["id"], UPDATE_INVALID_PARSER_CASES),
    ]:
        for config, _ in cases:
            before = sql_state(env)
            payload = {"parser_config": config, **({"name": "denied"} if method == "POST" else {})}
            response = request_api(env, method, path, credential=env["keys"][0], payload=payload)
            assert response.status_code == 422, response.text
            assert sql_state(env) == before
    invalid = [
        {"avatar": " "},
        {"avatar": []},
        {"avatar": "data:image/svg+xml;base64,abc"},
        {"avatar": "a" * 65536},
        {"name": " "},
        {"name": None},
        {"permission": "ME"},
        {"permission": None},
        {"chunk_method": "unknown"},
        {"pagerank": -1},
        {"pagerank": None},
        {"pagerank": True},
        {"description": "a" * 65536},
        {"embedding_model": "bad"},
        {"id": "forged"},
        *({"ext": {field: "forged"}} for field in ("id", "tenant_id", "created_by", "status")),
    ]
    for payload in invalid:
        before = sql_state(env)
        response = request_api(env, "POST", "/datasets", credential=env["keys"][0], payload={"name": "invalid-create", **payload})
        assert response.status_code == 422, response.text
        assert sql_state(env) == before
        before = sql_state(env)
        response = request_api(env, "PUT", "/datasets/" + data["id"], credential=env["keys"][0], payload=payload)
        assert response.status_code == 422, response.text
        assert sql_state(env) == before
    for extension in [{"chunk_token_num": 0}, {"built_in_metadata": [{"type": "string"}]}]:
        for wrapped in [False, True]:
            payload = {"parser_config": {"ext": extension}}
            if wrapped:
                payload = {"ext": payload}
            before = sql_state(env)
            response = request_api(env, "PUT", "/datasets/" + data["id"], credential=env["keys"][0], payload=payload)
            assert response.status_code == 422, response.text
            assert sql_state(env) == before


def test_update_partial_parser_avatar_omission_and_methods_http_sql(contracts: dict[str, Any]) -> None:
    env = contracts
    data = create(env, avatar="data:image/png;base64,abc", description="retain", parser_config={"raptor": {"max_token": 512}, "built_in_metadata": [{"key": "title"}]})
    path = "/datasets/" + data["id"]
    before = stored(env, data["id"])
    for patch in [{"description": "updated"}, {"avatar": ""}, {"avatar": None}, {"parser_config": {}}, {"parser_config": None}]:
        result = api(env, "PUT", path, patch)["data"]
        row = stored(env, data["id"])
        assert row["parser_config"] == before["parser_config"] == result["parser_config"]
        assert row["embd_id"] == before["embd_id"] and row["parser_id"] == before["parser_id"]
    for name, patch in UPDATE_VALID_PARSER_CASES:
        api(env, "PUT", path, {"parser_config": patch})
        row = stored(env, data["id"])
        for key, value in patch.items():
            if isinstance(value, dict):
                assert all(row["parser_config"][key][k] == v for k, v in value.items()), name
            else:
                assert row["parser_config"][key] == value, name
    for patch in [{"raptor": {"future_key": 0}}, {"built_in_metadata": []}, {"future_key": []}]:
        original = copy.deepcopy(stored(env, data["id"])["parser_config"])
        api(env, "PUT", path, {"parser_config": patch})
        actual = stored(env, data["id"])["parser_config"]
        for key in original.keys() - patch.keys():
            assert actual[key] == original[key]
        for key, value in patch.items():
            assert actual[key] == ({**original.get(key, {}), **value} if isinstance(value, dict) else value)
    api(env, "PUT", path, {"chunk_method": "qa", "parser_config": None})
    assert stored(env, data["id"])["parser_config"] == {"table_context_size": 0, "image_context_size": 0, "raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}}
    api(env, "PUT", path, {"language": "English", "connectors": [], "permission": "team"})
    assert stored(env, data["id"])["language"] == "English"
    assert stored(env, data["id"])["permission"] == "team"


def test_real_auth_foreign_and_payload_denials(contracts: dict[str, Any]) -> None:
    env = contracts
    data = create(env)
    for method, path, payload in [
        ("GET", "/datasets", None),
        ("GET", "/datasets/" + data["id"], None),
        ("PUT", "/datasets/" + data["id"], {"name": "denied"}),
        ("DELETE", "/datasets", {"ids": [data["id"]]}),
        ("POST", "/datasets", {"name": "denied"}),
    ]:
        for credential in [None, "invalid-token"]:
            before = sql_state(env)
            response = request_api(env, method, path, credential=credential, payload=payload)
            assert response.status_code == 401 and response.json()["retcode"] == 109
            assert sql_state(env) == before
        if method in ["PUT", "DELETE"] or (method == "GET" and path != "/datasets"):
            before = sql_state(env)
            response = request_api(env, method, path, credential=env["keys"][1], payload=payload)
            assert response.status_code == 200 and response.json()["code"] == 102, response.text
            assert sql_state(env) == before
    import requests

    for method, path in [("POST", "/datasets"), ("PUT", "/datasets/" + data["id"]), ("DELETE", "/datasets")]:
        for content, content_type in [("invalid", "application/json"), ('"text"', "application/json"), ('{"name":"bad"}', "text/xml")]:
            before = sql_state(env)
            response = requests.request(method, env["base"] + "/api/v1" + path, headers={"Authorization": "Bearer " + env["keys"][0], "Content-Type": content_type}, data=content, timeout=30)
            assert response.status_code == 422, response.text
            assert sql_state(env) == before


def test_list_pagination_sort_filter_concurrency_sql(contracts: dict[str, Any]) -> None:
    env = contracts
    with ThreadPoolExecutor(max_workers=4) as pool:
        created = list(pool.map(lambda i: create(env, name=f"parallel_{i}"), range(8)))
    ids = {data["id"] for data in created}
    rows = sql_state(env)[Knowledgebase.__tablename__]
    assert len(ids) == 8 and ids <= rows.keys()
    assert {rows[identifier]["name"] for identifier in ids} == {f"parallel_{i}" for i in range(8)}
    before = sql_state(env)
    for page in [1, 2, 3, 4]:
        result = api(env, "GET", "/datasets", params={"keywords": "parallel_", "orderby": "name", "desc": "false", "page": page, "page_size": 3})
        assert result["total_datasets"] == 8
        assert [row["name"] for row in result["data"]] == [f"parallel_{i}" for i in range(8)][(page - 1) * 3 : page * 3]
    for data in created:
        result = api(env, "GET", "/datasets", params={"id": data["id"], "name": data["name"]})
        assert [row["id"] for row in result["data"]] == [data["id"]]
        assert api(env, "GET", "/datasets/" + data["id"])["data"]["id"] == data["id"]
    assert api(env, "GET", "/datasets", params={"id": created[0]["id"], "name": created[1]["name"]})["data"] == []
    for params, status in [({"page": 0}, 422), ({"page_size": 0}, 422), ({"desc": "unknown"}, 422), ({"orderby": "unknown"}, 200), ({"id": env["datasets"][2]}, 200)]:
        response = request_api(env, "GET", "/datasets", credential=env["keys"][0], params=params)
        assert response.status_code == status
        if status == 200:
            assert response.json()["code"] == 102
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: api(env, "GET", "/datasets", params={"keywords": "parallel_", "page_size": 100}), range(12)))
    assert all({row["id"] for row in result["data"]} == ids and result["total_datasets"] == 8 for result in results)
    assert sql_state(env) == before


def attach_files(env: dict[str, Any], documents: list[str]) -> None:
    record = env["contract_record"]
    files, links = [uuid4().hex for _ in documents], [uuid4().hex for _ in documents]
    record["files"].extend(files)
    record["links"].extend(links)
    env["save_record"]()
    with Session(env["engine"]) as db:
        for file, link, document in zip(files, links, documents, strict=True):
            db.add(
                File(id=file, parent_id=env["users"][0], tenant_id=env["users"][0], created_by=env["users"][0], name="contract.txt", type=FileType.VIRTUAL.value, source_type=FileSource.KNOWLEDGEBASE)
            )
            db.add(File2Document(id=link, file_id=file, document_id=document))
        db.commit()


def test_status_aliases_both_paths_match_independent_sql(contracts: dict[str, Any]) -> None:
    env = contracts
    dataset, owner = env["datasets"][0], env["users"][0]
    docs = [uuid4().hex for _ in range(5)]
    env["documents"].extend(docs)
    env["contract_record"]["documents"] = env["documents"]
    env["save_record"]()
    with Session(env["engine"]) as db:
        for index, identifier in enumerate(docs):
            db.add(Document(id=identifier, kb_id=dataset, created_by=owner, name=f"status_{index}.txt", parser_id="naive", type="txt", suffix="txt", run=str(index)))
        db.commit()
    attach_files(env, docs)
    before = sql_state(env)
    path = f"/datasets/{dataset}/documents"
    for alias in ["run", "run_status", "run[]", "run_status[]"]:
        for value, statuses in [(" done ", ["3"]), (" 0 ", ["0"]), ("", [str(i) for i in range(5)]), ("  ", [str(i) for i in range(5)])]:
            params = [(alias, value), ("keywords", "status_")]
            with env["engine"].connect() as db:
                expected = set(db.scalars(sa.select(Document.id).where(Document.id.in_(docs), Document.run.in_(statuses))))
            listed = api(env, "GET", path, params=params)["data"]
            aggregated = api(env, "GET", path, params=params + [("type", "filter")])["data"]
            assert {row["id"] for row in listed["docs"]} == expected
            assert listed["total"] == aggregated["total"] == len(expected)
            assert set(aggregated["filter"]["run_status"]) == set(statuses)
        for mode in [[], [("type", "filter")]]:
            response = request_api(env, "GET", path, credential=env["keys"][0], params=[(alias, " bogus ")] + mode)
            assert response.status_code == 200 and response.json() == {"code": 102, "message": "Invalid filter run status conditions: bogus"}
    mixed = api(env, "GET", path, params=[("run", " 0 "), ("run_status[]", "done"), ("run[]", ""), ("keywords", "status_")])["data"]
    assert {row["id"] for row in mixed["docs"]} == {docs[0], docs[3]} and mixed["total"] == 2
    assert sql_state(env) == before


def test_embedding_authorization_and_indexed_switch_real_sql(contracts: dict[str, Any]) -> None:
    env = contracts
    factory = "Ollama"
    model_names = ["contract_" + uuid4().hex for _ in range(3)]
    env["contract_record"]["model_names"] = model_names
    env["save_record"]()
    with Session(env["engine"]) as db:
        db.merge(LLMFactories(name=factory, tags="EMBEDDING", status="1"))
        for i, model in enumerate(model_names):
            db.add(LLM(fid=factory, llm_name=model, mdl_type="embedding", tags="EMBEDDING"))
            row = TenantLLM(tenant_id=env["users"][1 if i == 2 else 0], llm_factory=factory, llm_name=model, mdl_type="embedding", api_key="controlled-local-only")
            db.add(row)
            db.flush()
            env["contract_record"]["models"].append(row.id)
        db.execute(sa.update(Tenant).where(Tenant.id == env["users"][0]).values(embd_id=model_names[0] + "@" + factory))
        db.commit()
    data = create(env, embedding_model=model_names[0] + "@" + factory)
    dataset = data["id"]
    with env["engine"].begin() as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == dataset).values(chunk_num=5))
    result = api(env, "PUT", "/datasets/" + dataset, {"embedding_model": model_names[1] + "@" + factory})["data"]
    assert result["embedding_model"] == stored(env, dataset)["embd_id"] == model_names[1] + "@" + factory
    assert stored(env, dataset)["chunk_num"] == 5
    for model, fragment in [(model_names[2] + "@" + factory, "Unauthorized model"), ("missing@UnknownContractProvider", "Unsupported model")]:
        for method, path, payload in [("POST", "/datasets", {"name": "model-denied", "embedding_model": model}), ("PUT", "/datasets/" + dataset, {"embedding_model": model})]:
            before = sql_state(env)
            response = request_api(env, method, path, credential=env["keys"][0], payload=payload)
            assert response.status_code == 200 and response.json()["code"] == 102 and fragment in response.json()["message"], response.text
            assert sql_state(env) == before
    before = stored(env, dataset)
    api(env, "PUT", "/datasets/" + dataset, {"embedding_model": None})
    assert stored(env, dataset)["embd_id"] == before["embd_id"]


def test_pagerank_zero_store_scope_and_infinity_denial(contracts: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = contracts
    dataset, sibling = env["datasets"][:2]
    values = {dataset: 7, sibling: 11}
    calls: list[dict[str, Any]] = []

    class Store:
        def update(self, condition: dict[str, Any], update: dict[str, Any], index: Any, kb_id: str) -> bool:
            calls.append(condition)
            for key in values:
                if condition.get("kb_id") == key:
                    values[key] = update.get("pagerank_fea", 0)
            return True

    monkeypatch.setattr(settings, "docStoreConn", Store())
    monkeypatch.setenv("DOC_ENGINE", "elasticsearch")
    for value in [50, 100, 0]:
        api(env, "PUT", "/datasets/" + dataset, {"pagerank": value})
        assert stored(env, dataset)["pagerank"] == value
        assert calls[-1]["kb_id"] == dataset and values[sibling] == 11
    before = sql_state(env)
    monkeypatch.setenv("DOC_ENGINE", "infinity")
    denied = request_api(env, "PUT", "/datasets/" + dataset, credential=env["keys"][0], payload={"pagerank": 50})
    assert denied.status_code == 200 and denied.json() == {"code": 102, "message": "'pagerank' can only be set when doc_engine is elasticsearch"}
    assert sql_state(env) == before and len(calls) == 3


def test_delete_contract_real_sql_objects_and_indexes(contracts: dict[str, Any]) -> None:
    import io

    from minio import Minio
    from pymilvus import DataType, MilvusClient

    from common.config_utils import CONFIGS
    from core.nlp import search

    env = contracts
    owner, foreign = env["users"][:2]
    targets = env["datasets"][:3]
    name = "delete_contract_" + uuid4().hex
    collections = [search.index_name_one(user, name) for user in [owner, foreign]]
    record = env["contract_record"]
    record.update(collections=collections, buckets=targets, objects=["contract.txt"], folders=[uuid4().hex for _ in range(2)])
    record["files"].extend(record["folders"])
    env["save_record"]()  # Before any RPC or SQL write.
    cfg = CONFIGS["milvus"]
    reader = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
    cfg = CONFIGS["minio"]
    object_reader = Minio(cfg["host"], access_key=cfg["user"], secret_key=cfg["password"], secure=bool(cfg.get("secure", False)))
    assert settings.docStoreConn.db_type() == "milvus"
    # This fixture owns physical buckets; shared-bucket deployments have separate tests.
    assert not cfg.get("bucket"), "This physical bucket acceptance requires a local multi-bucket MinIO configuration"
    try:
        with Session(env["engine"]) as db:
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id.in_(targets)).values(name=name))
            for identifier, tenant in zip(record["folders"], [owner, foreign], strict=True):
                db.add(File(id=identifier, parent_id=tenant, tenant_id=tenant, created_by=tenant, name=name, type="folder", source_type=FileSource.KNOWLEDGEBASE))
            db.commit()
        attach_files(env, env["documents"][:3])
        for bucket in targets:
            object_reader.make_bucket(bucket)
            object_reader.put_object(bucket, "contract.txt", io.BytesIO(b"owned bytes"), len(b"owned bytes"))
        for collection in collections:
            schema = reader.create_schema(auto_id=False)
            schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=128)
            schema.add_field("kb_id", DataType.VARCHAR, max_length=128)
            schema.add_field("doc_id", DataType.VARCHAR, max_length=128)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=2)
            indexes = reader.prepare_index_params()
            indexes.add_index("vector", index_type="AUTOINDEX", metric_type="COSINE")
            reader.create_collection(collection, schema=schema, index_params=indexes, consistency_level="Strong")
        for i, dataset in enumerate(targets):
            reader.insert(collections[0 if i < 2 else 1], [{"pk": dataset, "kb_id": dataset, "doc_id": env["documents"][i], "vector": [0.25, 0.75]}])
        for collection in collections:
            reader.flush(collection)
        before = sql_state(env)
        for ids in [[targets[2], targets[0]], [targets[0], targets[2]], [targets[0], "nonexistent"]]:
            response = request_api(env, "DELETE", "/datasets", credential=env["keys"][0], payload={"ids": ids})
            assert response.status_code == 200 and response.json()["code"] == 102
            assert sql_state(env) == before
            assert all(object_reader.bucket_exists(bucket) for bucket in targets)
            assert all(reader.has_collection(collection) for collection in collections)
        duplicate = request_api(env, "DELETE", "/datasets", credential=env["keys"][0], payload={"ids": [targets[0], targets[0]]})
        assert duplicate.status_code == 422 and sql_state(env) == before
        for payload in [{}, {"ids": []}, {"ids": None}]:
            api(env, "DELETE", "/datasets", payload)
            assert sql_state(env) == before
        api(env, "DELETE", "/datasets", {"ids": targets[:2]})
        after = sql_state(env)
        assert set(after[Knowledgebase.__tablename__]) == {targets[2]}
        assert set(after[Document.__tablename__]) == {env["documents"][2]}
        assert not any(object_reader.bucket_exists(bucket) for bucket in targets[:2])
        assert not reader.has_collection(collections[0]) and reader.has_collection(collections[1])
        assert object_reader.bucket_exists(targets[2]) and object_reader.stat_object(targets[2], "contract.txt").size == len(b"owned bytes")
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(File).where(File.id == record["folders"][1])) == 1
            assert db.scalar(sa.select(sa.func.count()).select_from(File).where(File.id == record["folders"][0])) == 0
        repeated = request_api(env, "DELETE", "/datasets", credential=env["keys"][0], payload={"ids": targets[:2]})
        assert repeated.status_code == 200 and repeated.json()["code"] == 102
        assert sql_state(env) == after
    finally:
        for bucket in targets:
            if object_reader.bucket_exists(bucket):
                for item in object_reader.list_objects(bucket, recursive=True):
                    object_reader.remove_object(bucket, item.object_name)
                object_reader.remove_bucket(bucket)
            assert not object_reader.bucket_exists(bucket)
        for collection in collections:
            if reader.has_collection(collection):
                reader.drop_collection(collection)
            assert not reader.has_collection(collection)
        reader.close()
        record.update(buckets_absent=True, collections_absent=True)
        env["save_record"]()


def test_metadata_summary_patch_and_foreign_denial(contracts: dict[str, Any]) -> None:
    env = contracts
    dataset = env["datasets"][0]
    docs = [env["documents"][0], uuid4().hex, uuid4().hex]
    env["documents"].extend(docs[1:])
    env["contract_record"]["documents"] = env["documents"]
    env["save_record"]()
    with Session(env["engine"]) as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == dataset).values(name="metadata_" + uuid4().hex))
        for identifier in docs[1:]:
            db.add(Document(id=identifier, kb_id=dataset, created_by=env["users"][0], name="metadata.txt", parser_id="naive", type="txt"))
        db.commit()
    from api.db.services.document_ingest_recovery import recovery_key

    env["contract_record"]["redis"] = [recovery_key(identifier) for identifier in docs]
    env["save_record"]()
    for identifier, metadata in zip(docs, [{"tags": ["foo", "bar"], "author": "alice"}, {"tags": ["foo"], "author": "bob"}, {"tags": ["bar", "baz"], "author": ""}], strict=True):
        api(env, "PATCH", f"/datasets/{dataset}/documents/{identifier}", {"meta_fields": metadata})
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(DocumentMetadata.meta_fields).where(DocumentMetadata.id == identifier)) == metadata
    before = sql_state(env)
    summary = api(env, "GET", f"/datasets/{dataset}/metadata/summary")["data"]["summary"]
    counts = {key: dict(field["values"]) for key, field in summary.items()}
    assert counts["tags"] == {"foo": 2, "bar": 2, "baz": 1}
    assert counts["author"] == {"alice": 1, "bob": 1, "": 1}
    assert "None" not in counts["author"]
    for identifier, credential in [(dataset, env["keys"][1]), ("missing", env["keys"][0])]:
        response = request_api(env, "GET", f"/datasets/{identifier}/metadata/summary", credential=credential)
        assert response.status_code == 200 and response.json()["code"] == 102
    assert sql_state(env) == before


def test_bounded_concurrent_update_and_delete_readback(contracts: dict[str, Any]) -> None:
    env = contracts
    data = create(env)
    path = "/datasets/" + data["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        updates = list(pool.map(lambda i: api(env, "PUT", path, {"description": f"update_{i}"}), range(12)))
    assert len(updates) == 12 and stored(env, data["id"])["description"] in {f"update_{i}" for i in range(12)}
    targets = [create(env) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda row: api(env, "DELETE", "/datasets", {"ids": [row["id"]]}), targets))
    assert len(results) == 6
    rows = sql_state(env)[Knowledgebase.__tablename__]
    assert not {row["id"] for row in targets} & rows.keys()
    assert data["id"] in rows and set(env["datasets"][:3]) <= rows.keys()


def test_failed_embedding_update_does_not_rename_folder(contracts: dict[str, Any]) -> None:
    env = contracts
    data = create(env)
    identifier = uuid4().hex
    env["contract_record"]["files"].append(identifier)
    env["save_record"]()
    with Session(env["engine"]) as db:
        db.add(File(id=identifier, parent_id=env["users"][0], tenant_id=env["users"][0], created_by=env["users"][0], name=data["name"], type="folder", source_type=FileSource.KNOWLEDGEBASE))
        db.commit()
    before = sql_state(env)
    denied = request_api(env, "PUT", "/datasets/" + data["id"], credential=env["keys"][0], payload={"name": "must-not-rename", "embedding_model": "missing@UnknownContractProvider"})
    assert denied.status_code == 200 and denied.json()["code"] == 102
    assert sql_state(env) == before
    with env["engine"].connect() as db:
        assert db.scalar(sa.select(File.name).where(File.id == identifier)) == data["name"]
