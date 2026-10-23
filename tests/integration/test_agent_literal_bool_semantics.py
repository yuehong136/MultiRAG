"""Registered beta execution of literal parsing, bool loops and durable variables."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn  # noqa: F401 -- initialize before legacy Canvas asyncio compatibility
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from api.db.db_models import API4Conversation, APIToken, UserCanvas
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_update_release import read_state
from tests.support.agent_update_release import release_api as release_api


def _node(name: str, params: dict[str, Any] | None = None, *, upstream: list[str] | None = None, downstream: list[str] | None = None, parent: str | None = None) -> dict[str, Any]:
    return {"obj": {"component_name": name, "params": params or {}}, "upstream": upstream or [], "downstream": downstream or [], "parent_id": parent}


def _dsl(*, invalid_assignment: bool) -> dict[str, Any]:
    assignments = [{"variable": "Loop:repeat@flag", "operator": "clear"}, {"variable": "env.iterations", "operator": "+=", "parameter": 1}]
    if invalid_assignment:
        assignments.insert(0, {})
    return {
        "components": {
            "begin": _node("Begin", downstream=["DataOperations:parse"]),
            "DataOperations:parse": _node("DataOperations", {"operations": "literal_eval", "query": ["env.source"]}, upstream=["begin"], downstream=["Loop:repeat"]),
            "Loop:repeat": _node(
                "Loop",
                {
                    "loop_variables": [{"variable": "flag", "type": "boolean", "input_mode": "constant", "value": True}],
                    "maximum_loop_count": 3,
                    "loop_termination_condition": [{"variable": "Loop:repeat@flag", "operator": "is", "value": False, "input_mode": "constant"}],
                },
                upstream=["DataOperations:parse"],
                downstream=["VariableAssigner:final"],
            ),
            "LoopItem:start": _node("LoopItem", parent="Loop:repeat", downstream=["VariableAssigner:clear"]),
            "VariableAssigner:clear": _node("VariableAssigner", {"variables": assignments}, parent="Loop:repeat", upstream=["LoopItem:start"]),
            "VariableAssigner:final": _node(
                "VariableAssigner",
                {
                    "variables": [
                        {"variable": "env.number", "operator": "set", "parameter": 0},
                        {"variable": "env.boolean", "operator": "set", "parameter": False},
                        {"variable": "env.text", "operator": "set", "parameter": ""},
                        {"variable": "env.items", "operator": "remove_first"},
                    ]
                },
                upstream=["Loop:repeat"],
                downstream=["Message:done"],
            ),
            "Message:done": _node("Message", {"content": ["done"]}, upstream=["VariableAssigner:final"]),
        },
        "variables": {
            "source": {"type": "object", "value": {"flag": "False", "zero": "0", "empty": "''"}},
            "iterations": {"type": "number", "value": 0},
            "number": {"type": "number", "value": 9},
            "boolean": {"type": "boolean", "value": True},
            "text": {"type": "string", "value": "seed"},
            "items": {"type": "array<string>", "value": ["first", "last"]},
        },
        "path": [],
        "history": [],
        "retrieval": [],
    }


@pytest.fixture(scope="module", autouse=True)
def variable_resources(bootstrapped_engine: sa.Engine, request: pytest.FixtureRequest) -> Iterator[list[dict[str, Any]]]:
    cases: list[dict[str, Any]] = []
    yield cases
    agent_ids = [case["agent_id"] for case in cases]
    with Session(bootstrapped_engine) as db:
        remaining = {
            model.__name__: db.scalar(sa.select(sa.func.count()).select_from(model).where(column.in_(agent_ids)))
            for model, column in [(UserCanvas, UserCanvas.id), (API4Conversation, API4Conversation.dialog_id)]
        }
    residual_keys = [key.decode() for case in cases for identifier in [case["agent_id"], *case["owners"], *case["task_ids"]] for key in REDIS_CONN.REDIS.scan_iter(match=f"*{identifier}*")]
    result = {"database": bootstrapped_engine.url.database, "cases": cases, "remaining_rows": remaining, "remaining_redis_keys": residual_keys}
    if directory := request.config.getoption("--test-report-dir"):
        (Path(directory) / "variable-resources.json").write_text(json.dumps(result, indent=2) + "\n")
    assert not any(remaining.values()) and not residual_keys


@pytest.mark.parametrize("invalid_assignment", [False, True])
def test_beta_canvas_literal_bool_and_falsy_assignment_readback(release_api: dict[str, Any], variable_resources: list[dict[str, Any]], invalid_assignment: bool) -> None:
    env = release_api
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Boolean variables", "dsl": _dsl(invalid_assignment=invalid_assignment)}, timeout=30)
    assert created.status_code == 200 and created.json()["retcode"] == 0, created.text
    agent_id = created.json()["data"]["id"]
    beta = uuid4().hex
    with Session(env["engine"]) as db:
        token = db.scalar(sa.select(APIToken).where(APIToken.token == env["keys"][0]))
        token.beta = beta
        db.commit()
    variable_resources.append({"agent_id": agent_id, "owners": env["owners"], "task_ids": env["task_ids"], "listener": env["base"]})
    before = read_state(env, agent_id)
    response = env["client"].post(
        f"{env['base']}/api/v1/agentbots/{agent_id}/completions", headers={"Authorization": f"Bearer {beta}"}, json={"query": "run variable graph", "stream": False}, timeout=30
    )
    assert response.status_code == 200, response.text
    with Session(env["engine"]) as db:
        stored = db.scalar(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id))
        assert stored is not None and stored.round == 1
        snapshot, messages, error = stored.dsl, stored.message, stored.errors
    canvas = Canvas(snapshot)
    try:
        assert canvas.get_component_obj("DataOperations:parse").output("result") == [{"flag": False, "zero": 0, "empty": ""}]
        if invalid_assignment:
            assert response.json()["code"] != 0 and "data" not in response.json()
            assert "Variable is not complete" in error and [message["role"] for message in messages] == ["user"]
            expected = {"Loop:repeat@flag": True, "env.iterations": 0, "env.number": 9, "env.boolean": True, "env.text": "seed", "env.items": ["first", "last"]}
        else:
            assert response.json()["code"] == 0 and response.json()["data"]["data"]["content"] == "done", response.text
            assert not error and messages[-1]["content"] == "done"
            expected = {"Loop:repeat@flag": False, "env.iterations": 1, "env.number": 0, "env.boolean": False, "env.text": "", "env.items": ["last"]}
        for name, value in expected.items():
            actual = canvas.get_variable_value(name)
            assert actual == value and type(actual) is type(value), name
    finally:
        canvas._thread_pool.shutdown(wait=True)
    assert read_state(env, agent_id) == before
