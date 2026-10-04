"""Real Canvas/HTTP/PG/Redis acceptance of the final cancellation boundary.

Only EOF, persistence scheduling and Redis finish failure are controllable
boundaries. Components, authorization, lifecycle Lua and storage remain real."""

import asyncio
import json
import socket
import threading
import time
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
from api.db.db_models import API4Conversation
from core.utils.redis_conn import REDIS_CONN
from core.utils.task_runtime import read_binding
from tests.support.agent_update_release import message_dsl, read_state, update
from tests.support.agent_update_release import release_api as release_api
from tests.support.task_cancellation import cancel
from tests.support.task_cancellation import cancel_api as cancel_api
from tests.support.task_cancellation_terminal import terminal_case


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["first", "continued", "published", "draft"])
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
