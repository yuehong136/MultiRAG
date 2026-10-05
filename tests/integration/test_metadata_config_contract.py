"""Metadata configuration through real HTTP and independent SQL readback."""

from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.services import dataset_api_service
from api.db.db_models import Document, Knowledgebase, Task
from api.db.services import document_ingest_service
from common.metadata_utils import build_metadata_config, turn2jsonschema
from tests.support.dataset_management_http import management_api as management_api
from tests.support.dataset_management_http import metadata_path, request_api, sql_state

FIELDS = [{"key": "score", "type": "number", "enum": ["1.5", "2"], "future": {"keep": True}}, {"key": "tags", "type": "list", "enum": ["A", "B"]}]
BUILTIN = [{"key": "source", "type": "string", "enum": ["Paper"]}]


@pytest.mark.parametrize("kind", ["jwt", "keys"])
@pytest.mark.parametrize(
    "metadata",
    [
        [{"name": "author", "examples": ["Ada"], "future": {"keep": True}}],
        {"type": "object", "properties": {"year": {"type": "integer"}}, "required": ["year"], "additionalProperties": False},
    ],
)
def test_enabled_only_roundtrip_preserves_all_definitions(management_api: dict[str, Any], kind: str, metadata: list[dict[str, Any]] | dict[str, Any]) -> None:
    env = management_api
    dataset, key = env["datasets"][0], env[kind][0]
    original = {"metadata": metadata, "built_in_metadata": BUILTIN, "unknown": {"keep": 3}}
    with env["engine"].begin() as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == dataset).values(parser_config=original))
    before = sql_state(env)
    for enabled in [False, True, False]:
        response = request_api(env, "PUT", metadata_path(env), credential=key, payload={"enabled": enabled})
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        assert response.json()["data"]["enabled"] is enabled
        readback = request_api(env, "GET", metadata_path(env), credential=key)
        assert readback.status_code == 200 and readback.json()["code"] == 0, readback.text
        assert readback.json()["data"]["enabled"] is enabled
        expected_metadata = [{"key": "author", "examples": ["Ada"], "future": {"keep": True}}] if isinstance(metadata, list) else metadata
        assert readback.json()["data"]["metadata"] == expected_metadata
        assert readback.json()["data"]["built_in_metadata"] == BUILTIN
        after = sql_state(env)
        assert after[Knowledgebase.__tablename__][dataset]["parser_config"] == {**original, "enable_metadata": enabled}
        assert after[Document.__tablename__] == before[Document.__tablename__]


def test_toggle_validation_legacy_rejection_and_empty_noop(management_api: dict[str, Any]) -> None:
    env = management_api
    dataset, key = env["datasets"][0], env["keys"][0]
    original = {"enable_metadata": False, "metadata": FIELDS, "built_in_metadata": BUILTIN, "unknown": {"keep": 3}}
    with env["engine"].begin() as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == dataset).values(parser_config=original))
    path = metadata_path(env)
    before = sql_state(env)
    for payload in [
        {"enabled": None},
        {"fields": None},
        {"enabled": "false"},
        {"enabled": False, "unknown": []},
        {"fields": []},
        {"fields": FIELDS, "enabled": False},
        {"metadata": FIELDS, "fields": FIELDS},
    ]:
        response = request_api(env, "PUT", path, credential=key, payload=payload)
        assert response.status_code == 422, response.text
        assert sql_state(env) == before
    for method, endpoint, payload in [
        ("POST", "/datasets", {"name": "rejected_" + uuid4().hex, "auto_metadata_config": {"fields": FIELDS, "enabled": True}}),
        ("PUT", f"/datasets/{dataset}", {"auto_metadata_config": {"fields": FIELDS}}),
    ]:
        response = request_api(env, method, endpoint, credential=key, payload=payload)
        assert response.status_code == 422, response.text
        assert sql_state(env) == before
    response = request_api(env, "PUT", path, credential=key, payload={})
    assert response.status_code == 200 and response.json() == {"code": 0, "data": {"enabled": False, "metadata": FIELDS, "built_in_metadata": BUILTIN}}, response.text
    stored = sql_state(env)[Knowledgebase.__tablename__][dataset]["parser_config"]
    assert stored == original


def test_retired_metadata_routes_return_404_without_writes(management_api: dict[str, Any]) -> None:
    env = management_api
    before = sql_state(env)
    path = f"/datasets/{env['datasets'][0]}/auto_metadata"
    for method in ["GET", "PUT"]:
        response = request_api(env, method, path, credential=env["keys"][0], payload={"enabled": True, "fields": []} if method == "PUT" else None)
        assert response.status_code == 404, response.text
        assert sql_state(env) == before


@pytest.mark.parametrize("kind", ["jwt", "keys"])
def test_new_config_roundtrip_partial_and_explicit_clear(management_api: dict[str, Any], kind: str) -> None:
    env = management_api
    dataset, key = env["datasets"][0], env[kind][0]
    original = {
        "enable_metadata": False,
        "metadata": [{"name": "author", "type": "string", "examples": ["Ada"], "restrict_values": False}],
        "built_in_metadata": BUILTIN,
        "unknown": {"keep": 3},
        "raptor": {"use_raptor": False, "scope": "dataset"},
    }
    with env["engine"].begin() as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == dataset).values(parser_config=original, pipeline_id="a" * 32))
    before = sql_state(env)
    for patch in [{"metadata": FIELDS}, {"metadata": FIELDS, "enabled": True}, {"built_in_metadata": []}, {"metadata": []}, {"metadata": FIELDS, "built_in_metadata": BUILTIN, "enabled": False}]:
        response = request_api(env, "PUT", metadata_path(env), credential=key, payload=patch)
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        original.update({name: deepcopy(value) for name, value in patch.items() if name != "enabled"})
        if "enabled" in patch:
            original["enable_metadata"] = patch["enabled"]
        reloaded = request_api(env, "GET", metadata_path(env), credential=key)
        assert reloaded.status_code == 200 and reloaded.json() == response.json()
        assert reloaded.json()["data"]["metadata"] == original["metadata"]
        assert reloaded.json()["data"]["built_in_metadata"] == original["built_in_metadata"]
        after = sql_state(env)
        assert after[Knowledgebase.__tablename__][dataset]["parser_config"] == original
        assert after[Knowledgebase.__tablename__][dataset]["pipeline_id"] == "a" * 32
        assert after[Document.__tablename__] == before[Document.__tablename__]
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id.in_(env["documents"]))) == 0
    schema = turn2jsonschema(build_metadata_config(original))
    assert schema["properties"]["score"]["type"] == "number"
    assert schema["properties"]["score"]["enum"] == [1.5, 2]
    assert schema["properties"]["tags"]["items"]["enum"] == ["A", "B"]
    assert schema["properties"]["source"]["enum"] == ["Paper"]


@pytest.mark.parametrize("field", ["auto_metadata_config", "parser_config"])
def test_create_and_update_use_same_config_contract(management_api: dict[str, Any], field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    env = management_api

    def scratch_session() -> Session:
        return Session(env["engine"])

    monkeypatch.setattr(dataset_api_service, "db_connection", scratch_session)
    key = env["keys"][0]
    config = {"metadata": FIELDS, "built_in_metadata": BUILTIN}
    config["enabled" if field == "auto_metadata_config" else "enable_metadata"] = False
    response = request_api(env, "POST", "/datasets", credential=key, payload={"name": "metadata_" + uuid4().hex, field: config})
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    dataset = response.json()["data"]["id"]
    env["datasets"].append(dataset)
    stored = sql_state(env)[Knowledgebase.__tablename__][dataset]["parser_config"]
    assert stored["metadata"] == FIELDS and stored["built_in_metadata"] == BUILTIN and stored["enable_metadata"] is False
    reloaded = request_api(env, "GET", f"/datasets/{dataset}", credential=key).json()
    assert reloaded["code"] == 0 and reloaded["data"]["parser_config"] == stored
    # Dataset update uses its existing PUT, including the nested config shape.
    updated = request_api(env, "PUT", f"/datasets/{dataset}", credential=key, payload={field: {"metadata": []}})
    assert updated.status_code == 200 and updated.json()["code"] == 0, updated.text
    readback = sql_state(env)[Knowledgebase.__tablename__][dataset]["parser_config"]
    assert readback == {**stored, "metadata": []}


def test_schema_roundtrip_and_invalid_payloads_are_atomic(management_api: dict[str, Any]) -> None:
    env = management_api
    key, path = env["keys"][0], metadata_path(env)
    schema = {"type": "object", "properties": {"year": {"type": "integer", "minimum": 1900}}, "required": ["year"], "additionalProperties": False}
    response = request_api(env, "PUT", path, credential=key, payload={"metadata": schema, "built_in_metadata": BUILTIN})
    assert response.status_code == 200 and response.json()["code"] == 0
    assert request_api(env, "GET", path, credential=key).json()["data"]["metadata"] == schema
    before = sql_state(env)
    assert before[Knowledgebase.__tablename__][env["datasets"][0]]["parser_config"]["metadata"] == schema
    for payload in [{"metadata": None}, {"metadata": [], "fields": FIELDS}, {"metadata": [{"key": "score", "type": "number", "enum": ["bad"]}]}, {"metadata": [{"key": "x", "type": "unknown"}]}]:
        response = request_api(env, "PUT", path, credential=key, payload=payload)
        assert response.status_code == 422 and sql_state(env) == before


def test_document_types_patch_and_schema_replacement(management_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = management_api

    def scratch_session() -> Session:
        return Session(env["engine"])

    monkeypatch.setattr(document_ingest_service, "db_connection", scratch_session)
    key, dataset, document = env["keys"][0], env["datasets"][0], env["documents"][0]
    path = f"/datasets/{dataset}/documents/{document}"
    with env["engine"].begin() as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == dataset).values(name="metadata_" + dataset))
    before = sql_state(env)
    original = before[Document.__tablename__][document]["parser_config"]
    fields = [{"key": "year", "type": "number", "enum": ["2026"]}]
    response = request_api(env, "PATCH", path, credential=key, payload={"parser_config": {"metadata": fields, "built_in_metadata": BUILTIN, "enable_metadata": False}})
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    stored = sql_state(env)[Document.__tablename__][document]
    assert stored["parser_config"] == {**original, "metadata": fields, "built_in_metadata": BUILTIN, "enable_metadata": False}
    assert stored["parser_id"] == before[Document.__tablename__][document]["parser_id"]
    assert stored["pipeline_id"] == before[Document.__tablename__][document]["pipeline_id"]
    for metadata in [
        {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "string"}}, "required": ["a"]},
        {"type": "object", "properties": {"a": {"type": "number"}}, "required": ["a"]},
        [],
    ]:
        response = request_api(env, "PUT", path + "/metadata/config", credential=key, payload={"metadata": metadata})
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        assert sql_state(env)[Document.__tablename__][document]["parser_config"] == {**stored["parser_config"], "metadata": metadata}
    assert sql_state(env)[Knowledgebase.__tablename__] == before[Knowledgebase.__tablename__]
    with env["engine"].connect() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id == document)) == 0
