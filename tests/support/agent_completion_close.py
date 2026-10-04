"""Actual HTTP/socket closure with controlled ASGI send or Canvas EOF waits.

The send wait holds a real response iterator at its yield; it does not claim
kernel buffer saturation. Auth, Canvas, PostgreSQL and Redis remain real.
"""

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
import uvicorn  # noqa: F401 -- initialize before legacy nest_asyncio patches
from fastapi.responses import StreamingResponse
from requests import Session as HTTPSession
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from api.db.db_models import API4Conversation, APIToken
from core.utils.redis_conn import REDIS_CONN
from core.utils.task_runtime import read_binding
from tests.support.agent_update_release import message_dsl, read_state, update
from tests.support.agent_update_release import release_api as release_api
from tests.support.task_cancellation import cancel
from tests.support.task_cancellation import cancel_api as cancel_api
from tests.support.task_cancellation_terminal import assert_failed_history


def close_case(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, *, surface: str, mode: str, window: str, winner: str, cancel_base: str | None = None) -> None:
    agent_id = env["client"].post(env["base"] + "/api/v1/agents", json={"title": f"close {uuid4().hex}", "dsl": message_dsl("real answer")}, timeout=30).json()["data"]["id"]
    update(env, agent_id, {"dsl": message_dsl("real answer"), "release": True})
    if mode in {"published", "draft"}:
        update(env, agent_id, {"dsl": message_dsl("different draft")})
    payload: dict[str, Any] = {"query": "close window", "release": surface != "debug", "stream": True}
    headers = dict(env["client"].headers)
    path = "/api/v1/agents/chat/completion"
    if surface == "beta":
        path = f"/api/v1/agentbots/{agent_id}/completions"
        with Session(env["engine"]) as db:
            token = db.scalar(sa.select(APIToken).where(APIToken.token == env["keys"][0]))
            token.beta = uuid4().hex
            db.commit()
            headers["Authorization"] = f"Bearer {token.beta}"
    else:
        payload["agent_id"] = agent_id
    if surface == "openai":
        payload.update({"openai-compatible": True, "messages": [{"role": "user", "content": "close window"}]})
    previous: list[dict[str, Any]] = []
    prior_dsl: dict[str, Any] = {"history": [], "globals": {"sys.history": []}}
    prior_round = 0
    with HTTPSession() as client:
        if mode == "draft":
            created = env["client"].post(env["base"] + f"/api/v1/agents/{agent_id}/sessions", json={"release": False}, timeout=30)
            assert created.status_code == 200 and created.json()["retcode"] == 0
            payload["session_id"] = created.json()["data"]["id"]
            payload["release"] = False
            with Session(env["engine"]) as db:
                row = db.get(API4Conversation, payload["session_id"])
                previous, prior_round = deepcopy(row.message), row.round
                prior_dsl = deepcopy(row.dsl) if isinstance(row.dsl, dict) else json.loads(row.dsl)
        if mode == "continued":
            first = client.post(env["base"] + path, headers=headers, json={**payload, "stream": False}, timeout=30)
            assert first.status_code == 200
            with Session(env["engine"]) as db:
                row = db.scalar(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id))
                assert row and not row.errors and row.message[-1]["role"] == "assistant"
                payload["session_id"], previous, prior_round = row.id, deepcopy(row.message), row.round
                prior_dsl = json.loads(row.dsl)
        before = read_state(env, agent_id)
        replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
        replica = REDIS_CONN.REDIS.get(replica_key)
        entered, released, response_closed, canvas_closed = (threading.Event() for _ in range(4))
        gate: dict[str, Any] = {}
        actual_run, actual_stream = Canvas.run, StreamingResponse.stream_response

        async def run(self: Canvas, **kwargs: Any) -> Any:
            gate.update(task_id=self.task_id, session_id=getattr(self, "artifact_session_id", None))
            try:
                async with aclosing(actual_run(self, **kwargs)) as output:
                    async for event in output:
                        yield event
                if window == "canvas":
                    entered.set()
                    assert await asyncio.to_thread(released.wait, 20)
            except BaseException as error:
                gate["canvas_close_exception"] = type(error).__name__
                raise
            finally:
                canvas_closed.set()

        async def stream(self: StreamingResponse, send: Any) -> None:
            # Retain the iterator so GC cannot impersonate response-owned close.
            gate["response"] = self

            async def controlled_send(message: dict[str, Any]) -> None:
                body = message.get("body", b"")
                selected = False
                if window == "send" and body.startswith(b"data:") and not entered.is_set():
                    value = body[5:].strip()
                    if value != b"[DONE]":
                        frame = json.loads(value)
                        if surface == "openai":
                            content = frame.get("choices", [{}])[0].get("delta", {}).get("content")
                            selected = bool(content) if winner == "cancel" else content == "" and read_binding(gate["task_id"])[1]["state"] == "finished"
                        else:
                            selected = frame.get("event") == ("message" if winner == "cancel" else "message_end")
                await send(message)
                if selected:
                    entered.set()
                    assert await asyncio.to_thread(released.wait, 20)

            try:
                await actual_stream(self, controlled_send)
            finally:
                response_closed.set()

        with monkeypatch.context() as patch:
            patch.setattr(Canvas, "run", run)
            patch.setattr(StreamingResponse, "stream_response", stream)
            response = client.post(env["base"] + path, headers=headers, json=payload, stream=True, timeout=30)
            iterator = response.iter_lines(chunk_size=1)
            try:
                assert response.status_code == 200
                assert next(line for line in iterator if line.startswith(b"data:"))
                assert entered.wait(10), "actual response did not reach the controlled closure window"
                task_id = gate["task_id"]
                replica_at_gate = REDIS_CONN.REDIS.get(replica_key)
                if surface == "debug":
                    with Session(env["engine"]) as db:
                        assert db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.dialog_id == agent_id)) == 0
                    assert (replica_at_gate != replica) is (winner == "finish")
                cancel_logs = {key: REDIS_CONN.REDIS.get(key) for key in REDIS_CONN.REDIS.scan_iter(match=f"{task_id}*-logs")}
                if cancel_base:
                    result = env["client"].post(cancel_base + f"/api/v1/tasks/{task_id}/cancel", headers={"Authorization": f"Bearer {env['keys'][0]}"}, timeout=30)
                    assert result.json()["code"] == 0
                else:
                    assert cancel(env, task_id).json()["retcode"] == 0
                binding_before_close = read_binding(task_id)
                assert binding_before_close[1]["state"] == ("cancel_requested" if winner == "cancel" else "finished")
                nonce = REDIS_CONN.REDIS.get(f"{task_id}-cancel")
                assert bool(nonce) is (winner == "cancel")
                if winner == "finish":
                    assert {key: REDIS_CONN.REDIS.get(key) for key in REDIS_CONN.REDIS.scan_iter(match=f"{task_id}*-logs")} == cancel_logs
                if surface != "debug" and winner == "finish":
                    with Session(env["engine"]) as db:
                        committed = deepcopy(db.get(API4Conversation, gate["session_id"]).to_dict())
                connection = response.raw._connection
                assert connection is not None and connection.sock is not None
                connection.sock.shutdown(socket.SHUT_RDWR)
                response.close()
                iterator.close()
                assert response_closed.wait(5), "ASGI did not observe actual socket disconnect"
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if surface == "debug":
                        if REDIS_CONN.REDIS.exists(f"{task_id}-cancel") and canvas_closed.is_set():
                            break
                    else:
                        with Session(env["engine"]) as db:
                            row = db.get(API4Conversation, gate["session_id"])
                            if row.round == prior_round + 1 and (winner == "finish" or row.errors) and REDIS_CONN.REDIS.exists(f"{task_id}-cancel"):
                                break
                    time.sleep(0.05)
                assert canvas_closed.is_set(), "response left actual Canvas iterator open"
                if winner == "cancel":
                    assert gate["canvas_close_exception"] == ("GeneratorExit" if window == "send" else "CancelledError")
                if surface != "debug":
                    assert row.round == prior_round + 1 and row.message[: len(previous)] == previous
                    if winner == "cancel":
                        assert [m["role"] for m in row.message[len(previous) :]] == ["user"]
                        assert "canceled" in row.errors and "disconnected" in row.errors
                        assert_failed_history(row.dsl, prior_dsl, "close window", env["owners"][0], agent_id)
                    else:
                        assert row.to_dict() == committed
                        assert not row.errors and row.message[-1]["role"] == "assistant"
                assert REDIS_CONN.REDIS.get(f"{task_id}-cancel") == (nonce if winner == "cancel" else "x")
                ttl = REDIS_CONN.REDIS.ttl(f"{task_id}-cancel")
                assert ttl > 86000 if winner == "cancel" else 3590 <= ttl <= 3600
                assert read_binding(task_id) == binding_before_close
                assert REDIS_CONN.REDIS.ttl(f"task-runtime:v1:{task_id}") > 86000
                assert read_state(env, agent_id) == before
                if surface != "debug" or winner == "cancel":
                    assert REDIS_CONN.REDIS.get(replica_key) == replica
                else:
                    assert REDIS_CONN.REDIS.get(replica_key) == replica_at_gate
                resumed_history: list[list[dict[str, Any]]] = []

                async def resume(self: Canvas, **kwargs: Any) -> Any:
                    resumed_history.append(deepcopy(self.get_history(100)))
                    async with aclosing(actual_run(self, **kwargs)) as output:
                        async for event in output:
                            yield event

                patch.setattr(Canvas, "run", resume)
                patch.setattr(StreamingResponse, "stream_response", actual_stream)
                retry = client.post(env["base"] + path, headers=headers, json={**payload, "session_id": gate.get("session_id"), "stream": False}, timeout=30)
                assert retry.status_code == 200
                if surface != "debug":
                    body = retry.json()
                    assert "choices" in body if surface == "openai" else body.get("retcode", body.get("code")) == 0
                    assert resumed_history == [Canvas(row.dsl, env["owners"][0], canvas_id=agent_id).get_history(100)]
                assert read_state(env, agent_id) == before
            finally:
                released.set()
                response.close()
                iterator.close()
    print(f"actual TCP/ASGI close: {surface}/{mode}/{window}/{winner}; SQL history, restored get_history, round, nonce and definition isolation verified")
