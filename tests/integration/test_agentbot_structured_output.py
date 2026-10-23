"""Beta auth, registered HTTP, real Canvas/SQL/Redis and controlled node output."""

import copy
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn  # noqa: F401 -- initialize before legacy Canvas asyncio compatibility
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.component.message import Message
from api.db.db_models import API4Conversation, APIToken, Tenant, User, UserCanvas, UserCanvasVersion, UserTenant
from api.db.services.api_service import API4ConversationService
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_update_release import message_dsl, read_state, sse_events, update
from tests.support.agent_update_release import release_api as release_api


@pytest.fixture(scope="module", autouse=True)
def resource_ledger(bootstrapped_engine: sa.Engine, request: pytest.FixtureRequest) -> Iterator[list[dict[str, Any]]]:
    cases: list[dict[str, Any]] = []
    yield cases
    owners = [owner for case in cases for owner in case["owners"]]
    with Session(bootstrapped_engine) as db:
        remaining = {
            model.__name__: db.scalar(sa.select(sa.func.count()).select_from(model).where(column.in_(owners)))
            for model, column in [(UserCanvas, UserCanvas.user_id), (Tenant, Tenant.id), (User, User.id), (UserTenant, UserTenant.user_id), (APIToken, APIToken.tenant_id)]
        }
    residual_keys = [key.decode() for case in cases for identifier in [*case["owners"], *case["task_ids"], *case["agent_ids"]] for key in REDIS_CONN.REDIS.scan_iter(match=f"*{identifier}*")]
    result = {"database": bootstrapped_engine.url.database, "cases": cases, "remaining_rows": remaining, "remaining_redis_keys": residual_keys}
    if directory := request.config.getoption("--test-report-dir"):
        (Path(directory) / "agentbot-resources.json").write_text(json.dumps(result, indent=2) + "\n")
    assert not any(remaining.values()) and not residual_keys


def _agent(env: dict[str, Any], ledger: list[dict[str, Any]], dsl: dict[str, Any] | None = None) -> tuple[str, dict[str, str]]:
    response = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Beta structured output", "dsl": dsl or message_dsl("published answer")}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    agent_id = response.json()["data"]["id"]
    beta = uuid4().hex
    with Session(env["engine"]) as db:
        token = db.scalar(sa.select(APIToken).where(APIToken.token == env["keys"][0]))
        token.beta = beta
        db.commit()
    ledger.append({"owners": env["owners"], "agent_ids": [agent_id], "task_ids": env["task_ids"], "listener": env["base"]})
    return agent_id, {"Authorization": f"Bearer {beta}"}


def _stored(env: dict[str, Any], agent_id: str) -> dict[str, Any]:
    with Session(env["engine"]) as db:
        rows = list(db.scalars(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id)))
        assert len(rows) == 1
        return copy.deepcopy(rows[0].to_dict())


@pytest.mark.parametrize("stream", [False, True])
def test_beta_http_structured_results_match_canvas_and_committed_session(release_api: dict[str, Any], resource_ledger: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    env = release_api
    agent_id, headers = _agent(env, resource_ledger)
    update(env, agent_id, {"dsl": message_dsl("published answer"), "release": True})
    update(env, agent_id, {"dsl": message_dsl("new draft")})
    before = read_state(env, agent_id)
    reference = {"chunks": {"0": {"chunk_id": "controlled-source", "content_with_weight": "controlled reference"}}, "doc_aggs": [{"doc_id": "controlled-doc"}]}
    original_invoke, original_run = Message._invoke, Canvas.run
    closed: list[str] = []

    def node_output(self: Message, **kwargs: Any) -> None:
        original_invoke(self, **kwargs)
        self.set_output("structured", {"count": 2, "approved": False})
        self._canvas.retrieval.append(copy.deepcopy(reference))

    async def track_close(self: Canvas, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        try:
            async with aclosing(original_run(self, **kwargs)) as events:
                async for event in events:
                    yield event
        finally:
            closed.append(self.task_id)

    monkeypatch.setattr(Message, "_invoke", node_output)
    monkeypatch.setattr(Canvas, "run", track_close)
    response = env["client"].post(
        f"{env['base']}/api/v1/agentbots/{agent_id}/completions?release=true", headers=headers, json={"query": "beta question", "stream": stream, "return_trace": True}, timeout=30
    )
    assert response.status_code == 200, response.text
    if stream:
        events = sse_events(response.text)
        assert not any(event.get("event") == "error" for event in events)
        assert "".join(event["data"]["content"] for event in events if event.get("event") == "message") == "published answer"
        assert next(event for event in events if event.get("event") == "node_finished" and event["data"]["component_id"] == "Message:answer")["data"]["outputs"]["structured"] == {
            "count": 2,
            "approved": False,
        }
    else:
        assert response.json()["code"] == 0, response.text
        data = response.json()["data"]["data"]
        assert data["content"] == "published answer" and data["reference"] == reference
        assert data["structured"] == {"Message:answer": {"count": 2, "approved": False}}
        assert len(data["trace"]) == 2
    stored = _stored(env, agent_id)
    assert stored["message"][-1]["content"] == "published answer" and stored["round"] == 1 and not stored["errors"]
    assert stored["reference"][-1] == reference
    restored = Canvas(stored["dsl"])
    try:
        assert restored.get_component_obj("Message:answer").output("structured") == {"count": 2, "approved": False}
    finally:
        restored._thread_pool.shutdown(wait=True)
    assert closed == env["task_ids"][:1] and read_state(env, agent_id) == before


def test_beta_http_pause_form_survives_and_resumes_same_canvas(release_api: dict[str, Any], resource_ledger: list[dict[str, Any]]) -> None:
    env = release_api
    dsl = message_dsl("form complete")
    dsl["components"]["begin"]["downstream"] = ["UserFillUp:form"]
    dsl["components"]["Message:answer"]["upstream"] = ["UserFillUp:form"]
    dsl["components"]["UserFillUp:form"] = {
        "obj": {"component_name": "UserFillUp", "params": {"inputs": {"approval": {"type": "line", "name": "Approval", "value": None}}, "tips": "Approve"}},
        "upstream": ["begin"],
        "downstream": ["Message:answer"],
    }
    agent_id, headers = _agent(env, resource_ledger, dsl)
    url = f"{env['base']}/api/v1/agentbots/{agent_id}/completions"
    first = env["client"].post(url, headers=headers, json={"query": "start", "stream": False}, timeout=30)
    assert first.json()["code"] == 0, first.text
    event = first.json()["data"]
    assert event["event"] == "user_inputs" and event["data"]["inputs"]["approval"]["name"] == "Approval" and event["data"]["tips"] == "Approve"
    stored = _stored(env, agent_id)
    assert json.loads(stored["dsl"])["path"][0] == "UserFillUp:form" and stored["message"][-1]["runtime_pause"]["tips"] == "Approve"
    second = env["client"].post(url, headers=headers, json={"session_id": stored["id"], "query": "resume", "stream": False, "inputs": {"approval": {"type": "line", "value": "yes"}}}, timeout=30)
    assert second.json()["code"] == 0 and second.json()["data"]["data"]["content"] == "form complete", second.text
    assert _stored(env, agent_id)["round"] == 2


@pytest.mark.parametrize("failure", ["empty", "exception", "event", "write"])
def test_beta_http_failure_never_commits_success(release_api: dict[str, Any], resource_ledger: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    env = release_api
    agent_id, headers = _agent(env, resource_ledger)
    before = read_state(env, agent_id)
    original_run = Canvas.run

    async def faulty_run(self: Canvas, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        if failure == "empty":
            return
        async with aclosing(original_run(self, **kwargs)) as events:
            async for event in events:
                yield event
                if failure == "exception" and event["event"] == "message":
                    raise RuntimeError("controlled beta failure")
        if failure == "event":
            yield {"event": "error", "data": {"error": "controlled late failure"}}

    monkeypatch.setattr(Canvas, "run", faulty_run)
    if failure == "write":
        append_message = API4ConversationService.append_message

        def fail_success(cls: type[API4ConversationService], db: Session, cid: str, conv: dict[str, Any]) -> int:
            return append_message(db, cid, conv) if conv.get("errors") else 0

        monkeypatch.setattr(API4ConversationService, "append_message", classmethod(fail_success))
    response = env["client"].post(f"{env['base']}/api/v1/agentbots/{agent_id}/completions", headers=headers, json={"query": "failure", "stream": False}, timeout=30)
    assert response.json()["code"] != 0 and "data" not in response.json(), response.text
    stored = _stored(env, agent_id)
    assert stored["errors"] and stored["round"] == 1 and [message["role"] for message in stored["message"]] == ["user"]
    assert read_state(env, agent_id) == before


def test_beta_http_auth_and_foreign_owner_denial_have_no_session_writes(release_api: dict[str, Any], resource_ledger: list[dict[str, Any]]) -> None:
    env = release_api
    agent_id, _ = _agent(env, resource_ledger)
    foreign_beta = uuid4().hex
    with Session(env["engine"]) as db:
        token = db.scalar(sa.select(APIToken).where(APIToken.token == env["keys"][1]))
        token.beta = foreign_beta
        db.commit()
    before = read_state(env, agent_id)
    url = f"{env['base']}/api/v1/agentbots/{agent_id}/completions"
    for credential in ["", "Bearer invalid", f"Bearer {foreign_beta}"]:
        response = env["client"].post(url, headers={"Authorization": credential}, json={"query": "denied", "stream": False}, timeout=30)
        assert response.status_code in {200, 401, 403} and response.json().get("code", -1) != 0, response.text
    with Session(env["engine"]) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.dialog_id == agent_id)) == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(UserCanvasVersion).where(UserCanvasVersion.user_canvas_id == agent_id)) == len(before["versions"])
    assert not env["task_ids"] and read_state(env, agent_id) == before
