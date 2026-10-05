"""Real Dataset saves and independent PostgreSQL readbacks; no parsing is scheduled."""

from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.services import dataset_api_service
from api.db.db_models import Knowledgebase, Task
from tests.support.dataset_management_http import management_api as management_api
from tests.support.dataset_management_http import request_api, sql_state


@pytest.fixture
def scope_api(management_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # Dataset PUT uses its own thread/session; bind that factory to the same
    # scratch database as HTTP auth and reads instead of the configured database.
    def scratch_session() -> Session:
        return Session(management_api["engine"])

    monkeypatch.setattr(dataset_api_service, "db_connection", scratch_session)
    return management_api


def _task_count(env: dict[str, Any]) -> int:
    with env["engine"].connect() as db:
        return int(db.scalar(sa.select(sa.func.count()).select_from(Task)) or 0)


@pytest.mark.parametrize("kind", ["jwt", "keys"])
def test_scope_patch_save_reload_and_extensions(scope_api: dict[str, Any], kind: str) -> None:
    env = scope_api
    dataset = env["datasets"][0]
    path = f"/datasets/{dataset}"
    original = {
        "chunk_token_num": 321,
        "raptor": {"use_raptor": False, "scope": "dataset", "max_token": 123, "ext": {"future": False}, "unknown": {"value": 7}},
        "metadata": [{"key": "author", "enum": ["Alice"], "restrictDefinedValues": True}],
        "enable_metadata": True,
        "unknown_parser": {"retained": True},
        "parent_child": {},
    }
    with env["engine"].begin() as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == dataset).values(parser_config=original, pipeline_id="a" * 32))
    tasks_before = _task_count(env)
    before = sql_state(env)
    for scope in ["file", "dataset"]:
        response = request_api(env, "PUT", path, credential=env[kind][0], payload={"parser_config": {"raptor": {"scope": scope}}})
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        expected = {**original, "raptor": {**original["raptor"], "scope": scope}}
        assert response.json()["data"]["parser_config"] == expected
        reloaded = request_api(env, "GET", path, credential=env[kind][0])
        assert reloaded.status_code == 200 and reloaded.json()["code"] == 0
        assert reloaded.json()["data"]["parser_config"] == expected
        stored = sql_state(env)
        assert stored[Knowledgebase.__tablename__][dataset]["parser_config"] == expected
        assert stored[Knowledgebase.__tablename__][dataset]["pipeline_id"] == "a" * 32
        for other in env["datasets"][1:]:
            assert stored[Knowledgebase.__tablename__][other] == before[Knowledgebase.__tablename__][other]
        for table in before.keys() - {Knowledgebase.__tablename__}:
            assert stored[table] == before[table]
        assert _task_count(env) == tasks_before

    # Omitted scope and unrelated parser patches keep the previous generation scope.
    response = request_api(env, "PUT", path, credential=env[kind][0], payload={"parser_config": {"raptor": {"max_token": 234}}})
    assert response.status_code == 200 and response.json()["code"] == 0
    assert sql_state(env)[Knowledgebase.__tablename__][dataset]["parser_config"]["raptor"] == {**original["raptor"], "max_token": 234}
    # An explicit built-in parser choice retains configuration and exits Pipeline.
    response = request_api(env, "PUT", path, credential=env[kind][0], payload={"chunk_method": "qa", "parser_config": {"raptor": {"scope": "file"}}})
    assert response.status_code == 200 and response.json()["code"] == 0
    stored = sql_state(env)[Knowledgebase.__tablename__][dataset]
    assert stored["parser_id"] == "qa" and stored["pipeline_id"] == ""
    assert stored["parser_config"]["raptor"]["scope"] == "file"
    assert stored["parser_config"]["metadata"] == original["metadata"]
    assert _task_count(env) == tasks_before


@pytest.mark.parametrize("scope", ["all", "Dataset", "", None, 0])
def test_invalid_scope_patch_is_atomic(scope_api: dict[str, Any], scope: Any) -> None:
    env = scope_api
    before = sql_state(env)
    response = request_api(env, "PUT", f"/datasets/{env['datasets'][0]}", credential=env["keys"][0], payload={"description": "must not persist", "parser_config": {"raptor": {"scope": scope}}})
    assert response.status_code == 422 and "detail" in response.json()
    assert sql_state(env) == before


@pytest.mark.parametrize("scope", ["file", "dataset", None])
def test_scope_create_save_and_independent_reload(management_api: dict[str, Any], scope: str | None) -> None:
    env = management_api
    raptor: dict[str, Any] = {"use_raptor": False, "ext": {"future": {"enabled": False}}}
    if scope is not None:
        raptor["scope"] = scope
    tasks_before = _task_count(env)
    response = request_api(
        env,
        "POST",
        "/datasets",
        credential=env["keys"][0],
        payload={
            "name": "scope_" + uuid4().hex,
            "parser_config": {"raptor": raptor, "ext": {"future_parser": 7}},
            "auto_metadata_config": {"enabled": True, "metadata": [{"key": "author", "type": "string"}]},
        },
    )
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    dataset = response.json()["data"]["id"]
    env["datasets"].append(dataset)  # Existing fixture cleanup includes the new row.
    stored = sql_state(env)[Knowledgebase.__tablename__][dataset]["parser_config"]
    assert stored["raptor"]["scope"] == (scope or "file")
    assert stored["raptor"]["use_raptor"] is False
    assert stored["raptor"]["ext"] == raptor["ext"] and stored["ext"] == {"future_parser": 7}
    assert stored["enable_metadata"] is True and stored["metadata"][0]["key"] == "author"
    reloaded = request_api(env, "GET", f"/datasets/{dataset}", credential=env["keys"][0])
    assert reloaded.status_code == 200 and reloaded.json()["code"] == 0
    assert reloaded.json()["data"]["parser_config"] == stored
    assert _task_count(env) == tasks_before
