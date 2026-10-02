"""Real Canvas/HTTP/PG/Redis acceptance of the final cancellation boundary.

Only EOF, persistence scheduling and Redis finish failure are controllable
boundaries. Components, authorization, lifecycle Lua and storage remain real.
"""

import asyncio
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn  # noqa: F401 -- load before legacy nest_asyncio patches
from requests import Session as HTTPSession
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from api.db.db_models import API4Conversation, APIToken, Task
from core.utils.redis_conn import REDIS_CONN
from core.utils.task_runtime import FINISH_RUNTIME_SCRIPT, binding_key, read_binding
from tests.integration.test_agent_update_release import message_dsl, read_state, update
from tests.integration.test_agent_update_release import release_api as release_api
from tests.integration.test_task_cancellation import cancel
from tests.integration.test_task_cancellation import cancel_api as cancel_api


def events(response: Any) -> list[dict[str, Any]]:
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:") and line[5:].strip() != "[DONE]"]


def terminal_case(
    env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    surface: str,
    mode: str,
    stream: bool,
    window: str,
    winner: str,
    cancel_base: str | None = None,
) -> None:
    agent_id = env["client"].post(env["base"] + "/api/v1/agents", json={"title": f"terminal {uuid4().hex}", "dsl": message_dsl("real answer")}, timeout=30).json()["data"]["id"]
    update(env, agent_id, {"dsl": message_dsl("real answer"), "release": True})
    if mode == "published":
        update(env, agent_id, {"dsl": message_dsl("different draft")})
    headers = dict(env["client"].headers)
    if surface == "beta":
        with Session(env["engine"]) as db:
            token = db.scalar(sa.select(APIToken).where(APIToken.token == env["keys"][0]))
            token.beta = uuid4().hex
            db.commit()
            headers["Authorization"] = f"Bearer {db.scalar(sa.select(APIToken.beta).where(APIToken.token == env['keys'][0]))}"

    def request(session_id: str | None = None, streaming: bool = stream) -> Any:
        payload: dict[str, Any] = {"query": "final window", "release": surface != "debug", "stream": streaming}
        if session_id:
            payload["session_id"] = session_id
        path = f"/api/v1/agentbots/{agent_id}/completions" if surface == "beta" else "/api/v1/agents/chat/completion"
        if surface != "beta":
            payload["agent_id"] = agent_id
        if surface == "openai":
            payload.update({"openai-compatible": True, "messages": [{"role": "user", "content": "final window"}]})
        with HTTPSession() as client:
            return client.post(env["base"] + path, headers=headers, json=payload, timeout=30)

    session_id = None
    prior_messages: list[dict[str, Any]] = []
    prior_round = 0
    if mode == "continued":
        initial = request(streaming=False)
        assert initial.status_code == 200
        with Session(env["engine"]) as db:
            row = db.scalar(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id))
            assert row and row.message[-1]["role"] == "assistant" and not row.errors
            session_id, prior_messages, prior_round = row.id, deepcopy(row.message), row.round
    before = read_state(env, agent_id)
    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica = REDIS_CONN.REDIS.get(replica_key)
    entered, release = threading.Event(), threading.Event()
    gate: dict[str, Any] = {"task_id": None}
    original_run, original_bridge, original_eval = Canvas.run, AsyncSession.run_sync, REDIS_CONN.REDIS.eval

    async def actual_run(self: Canvas, **kwargs: Any) -> Any:
        if gate["task_id"] is None:
            gate["task_id"] = self.task_id
            gate["session_id"] = getattr(self, "artifact_session_id", None)
        async with aclosing(original_run(self, **kwargs)) as output:
            async for event in output:
                yield event
        if self.task_id == gate["task_id"] and window == "after_events":
            entered.set()
            assert await asyncio.to_thread(release.wait, 20)

    async def persistence_bridge(self: AsyncSession, fn: Any, *args: Any, **kwargs: Any) -> Any:
        if window == "persistence" and not entered.is_set() and "append_message" in fn.__code__.co_names:
            entered.set()
            assert await asyncio.to_thread(release.wait, 20)
        return await original_bridge(self, fn, *args, **kwargs)

    def finish_boundary(script: str, number: int, *args: Any) -> Any:
        if script == FINISH_RUNTIME_SCRIPT and args[0] == binding_key(gate["task_id"]) and window == "finish":
            entered.set()
            assert release.wait(20)
            if winner == "redis_failure":
                raise ConnectionError("test injected terminal Redis failure")
            if winner == "redis_false":
                return False
        return original_eval(script, number, *args)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
        patch.setattr(Canvas, "run", actual_run)
        patch.setattr(AsyncSession, "run_sync", persistence_bridge)
        patch.setattr(REDIS_CONN.REDIS, "eval", finish_boundary)
        future = pool.submit(request, session_id)
        try:
            assert entered.wait(15), "real run did not reach the final boundary"
            task_id = gate["task_id"]
            binding = read_binding(task_id)[1]
            assert binding["principal_id"] == env["owners"][0]
            with Session(env["engine"]) as db:
                assert db.get(Task, task_id) is None
                if surface != "debug":
                    row = db.get(API4Conversation, gate["session_id"])
                    # No successful append is allowed before the finish decision.
                    assert row.message == prior_messages and row.round == prior_round
            if winner not in {"redis_failure", "redis_false", "sql_failure"}:
                if cancel_base is None:
                    response = cancel(env, task_id)
                    assert response.json()["retcode"] == 0
                else:
                    response = env["client"].post(cancel_base + f"/api/v1/tasks/{task_id}/cancel", headers={"Authorization": f"Bearer {env['keys'][0]}"}, timeout=30)
                    assert response.json()["code"] == 0
                expected_state = "finished" if winner == "finish" else "cancel_requested"
                assert read_binding(task_id)[1]["state"] == expected_state
                assert bool(REDIS_CONN.REDIS.exists(f"{task_id}-cancel")) is (winner != "finish")
            nonce = REDIS_CONN.REDIS.get(f"{task_id}-cancel")
        finally:
            release.set()
        result = future.result(timeout=25)
    assert result.status_code == 200, result.text
    failed = winner != "finish"
    if stream or surface == "debug":
        output = events(result)
        if failed:
            assert any(event.get("event") == "error" or event.get("code", 0) != 0 or "error" in event for event in output), result.text
            assert not any(event.get("event") in {"message_end", "workflow_finished"} for event in output)
            assert "[DONE]" not in result.text
        elif surface == "openai":
            assert "[DONE]" in result.text and not any("error" in event for event in output)
        else:
            assert any(event.get("event") == "message_end" for event in output)
    else:
        body = result.json()
        if surface == "openai":
            assert ("error" in body) is failed and ("choices" in body) is not failed
        else:
            assert (body.get("retcode", body.get("code")) != 0) is failed
            if failed:
                assert not body.get("data")
    if winner == "cancel":
        assert read_binding(task_id)[1]["state"] == "cancel_requested"
        assert REDIS_CONN.REDIS.get(f"{task_id}-cancel") == nonce and REDIS_CONN.REDIS.ttl(f"{task_id}-cancel") > 86000
    elif winner in {"finish", "sql_failure"}:
        assert read_binding(task_id)[1]["state"] == "finished"
    else:
        assert read_binding(task_id)[1]["state"] == "active"  # Redis fault prevented terminal registration.
    if surface != "debug":
        with Session(env["engine"]) as db:
            row = db.get(API4Conversation, gate["session_id"])
            assert row.round == prior_round + 1 and row.message[: len(prior_messages)] == prior_messages
            assert row.message[len(prior_messages)]["role"] == "user"
            assert [message["role"] for message in row.message[len(prior_messages) :]] == (["user"] if failed else ["user", "assistant"])
            assert bool(row.errors) is failed
            if failed:
                expected_error = (
                    "canceled"
                    if winner == "cancel"
                    else "terminal SQL failure"
                    if winner == "sql_failure"
                    else "resolve task runtime completion"
                    if winner == "redis_false"
                    else "terminal Redis failure"
                )
                assert expected_error in row.errors
    assert read_state(env, agent_id) == before
    if surface != "debug" or failed:
        assert REDIS_CONN.REDIS.get(replica_key) == replica
    # An independent sibling still completes using the same actual Canvas.
    sibling = request(streaming=False)
    assert sibling.status_code == 200 and not any(event.get("event") == "error" for event in events(sibling))
    if surface != "debug":
        body = sibling.json()
        assert "choices" in body if surface == "openai" else body.get("retcode", body.get("code")) == 0
    with Session(env["engine"]) as db:
        if surface != "debug":
            siblings = list(db.scalars(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id, API4Conversation.id != gate["session_id"])))
            assert len(siblings) == 1 and not siblings[0].errors and siblings[0].message[-1]["role"] == "assistant"
    print(f"terminal HTTP: {surface}/{mode}/stream={stream}, {window}, winner={winner}; SQL round and lifecycle independently verified")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["first", "continued", "published"])
@pytest.mark.parametrize("surface", ["rest", "beta", "openai"])
def test_cancel_wins_after_last_event(cancel_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, surface: str, mode: str, stream: bool) -> None:
    terminal_case(cancel_api, monkeypatch, surface=surface, mode=mode, stream=stream, window="after_events", winner="cancel")


@pytest.mark.parametrize("winner,window", [("cancel", "finish"), ("finish", "persistence"), ("redis_failure", "finish"), ("redis_false", "finish")])
@pytest.mark.parametrize("stream", [False, True])
def test_finish_persistence_and_redis_failure(cancel_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, winner: str, window: str, stream: bool) -> None:
    terminal_case(cancel_api, monkeypatch, surface="rest", mode="continued", stream=stream, window=window, winner=winner)


@pytest.mark.parametrize("winner,window", [("cancel", "after_events"), ("cancel", "finish"), ("redis_failure", "finish")])
def test_debug_terminal_boundary(cancel_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, winner: str, window: str) -> None:
    terminal_case(cancel_api, monkeypatch, surface="debug", mode="first", stream=True, window=window, winner=winner)


@pytest.mark.parametrize("stream", [False, True])
def test_terminal_sql_failure_preserves_one_failed_round(cancel_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    fault = {"raised": False}

    def fail_sql(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if not fault["raised"] and "UPDATE usr_ai.t_ai_api4conversations" in statement:
            fault["raised"] = True
            raise RuntimeError("test injected terminal SQL failure")

    # Enable the fault only when the terminal gate releases, after the initial
    # continued round. The scheduling bridge itself still calls real SQL.
    original_bridge = AsyncSession.run_sync

    async def bridge(self: AsyncSession, fn: Any, *args: Any, **kwargs: Any) -> Any:
        if "append_message" in fn.__code__.co_names and not fault.get("armed"):
            fault["armed"] = True
        elif "append_message" in fn.__code__.co_names and not fault["raised"]:
            sa.event.listen(sa.engine.Engine, "before_cursor_execute", fail_sql)
        return await original_bridge(self, fn, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "run_sync", bridge)
    try:
        terminal_case(cancel_api, monkeypatch, surface="rest", mode="continued", stream=stream, window="persistence", winner="sql_failure")
    finally:
        if sa.event.contains(sa.engine.Engine, "before_cursor_execute", fail_sql):
            sa.event.remove(sa.engine.Engine, "before_cursor_execute", fail_sql)
    assert fault["raised"]


@pytest.mark.parametrize("continued", [False, True])
def test_real_disconnect_after_cancel_preserves_failure(cancel_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, continued: bool) -> None:
    env = cancel_api
    agent_id = env["client"].post(env["base"] + "/api/v1/agents", json={"title": f"disconnect {uuid4().hex}", "dsl": message_dsl("real answer")}, timeout=30).json()["data"]["id"]
    update(env, agent_id, {"dsl": message_dsl("real answer"), "release": True})
    payload: dict[str, Any] = {"agent_id": agent_id, "release": True, "stream": True, "query": "disconnect"}
    previous: list[dict[str, Any]] = []
    prior_round = 0
    if continued:
        initial = env["client"].post(env["base"] + "/api/v1/agents/chat/completion", json={**payload, "stream": False}, timeout=30)
        assert initial.json()["retcode"] == 0
        with Session(env["engine"]) as db:
            row = db.scalar(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id))
            payload["session_id"], previous, prior_round = row.id, deepcopy(row.message), row.round
    before = read_state(env, agent_id)
    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica = REDIS_CONN.REDIS.get(replica_key)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    gate: dict[str, Any] = {}
    original = Canvas.run

    async def actual_run(self: Canvas, **kwargs: Any) -> Any:
        gate.update(task_id=self.task_id, session_id=self.artifact_session_id)
        try:
            async with aclosing(original(self, **kwargs)) as output:
                async for event in output:
                    yield event
            entered.set()
            assert await asyncio.to_thread(release.wait, 15)
        finally:
            closed.set()

    monkeypatch.setattr(Canvas, "run", actual_run)
    try:
        with HTTPSession() as client:
            response = client.post(env["base"] + "/api/v1/agents/chat/completion", headers=env["client"].headers, json=payload, stream=True, timeout=30)
            connection = response.raw._connection
            assert connection is not None and connection.sock is not None
            owned_socket = connection.sock
            iterator = response.iter_lines(chunk_size=1)
            first = next(line for line in iterator if line.startswith(b"data:"))
            assert json.loads(first[5:])["event"] != "message_end"
            assert entered.wait(10)
            assert cancel(env, gate["task_id"]).json()["retcode"] == 0
            nonce = REDIS_CONN.REDIS.get(f"{gate['task_id']}-cancel")
            # A Response.close alone can leave the pooled TCP stream alive.
            # Shut down this owned socket so ASGI observes actual disconnect.
            owned_socket.shutdown(socket.SHUT_RDWR)
            response.close()
            iterator.close()
        assert closed.wait(5), "actual streaming generator did not observe disconnect"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with Session(env["engine"]) as db:
                row = db.get(API4Conversation, gate["session_id"])
                if row.errors:
                    break
            time.sleep(0.05)
        assert row.round == prior_round + 1 and row.message == previous + [row.message[-1]]
        assert row.message[-1]["role"] == "user" and "canceled" in (row.errors or "") and "disconnected" in row.errors
        assert read_binding(gate["task_id"])[1]["state"] == "cancel_requested"
        assert REDIS_CONN.REDIS.get(f"{gate['task_id']}-cancel") == nonce
        assert read_state(env, agent_id) == before and REDIS_CONN.REDIS.get(replica_key) == replica
    finally:
        release.set()
    print("actual client disconnect: cancellation winner persisted one failed round; delivery after close is not claimed")
