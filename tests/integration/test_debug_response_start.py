"""Real registered debug route, with a controlled pre-iteration ASGI send wait."""

import asyncio
import json
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn  # noqa: F401 -- initialize before legacy nest_asyncio patches
from fastapi.responses import StreamingResponse
from requests import Session as HTTPSession
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from api.db.db_models import API4Conversation, Task
from api.utils.agent_streaming import AgentStreamingResponse
from core.utils.redis_conn import REDIS_CONN
from core.utils.task_runtime import binding_key, read_binding
from tests.integration.test_agent_update_release import message_dsl, read_state
from tests.integration.test_agent_update_release import release_api as release_api
from tests.integration.test_task_cancellation import cancel
from tests.integration.test_task_cancellation import cancel_api as cancel_api


def debug_start_case(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, *, failure: str, cancelled: bool, cancel_base: str | None = None) -> None:
    created = env["client"].post(env["base"] + "/api/v1/agents", json={"title": f"debug start {uuid4().hex}", "dsl": message_dsl("real answer")}, timeout=30)
    assert created.status_code == 200 and created.json()["retcode"] == 0
    agent_id = created.json()["data"]["id"]
    fetched = env["client"].get(env["base"] + f"/api/v1/agents/{agent_id}", timeout=30)
    assert fetched.status_code == 200 and fetched.json()["retcode"] == 0
    before = read_state(env, agent_id)
    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica = REDIS_CONN.REDIS.get(replica_key)
    initial_tasks = len(env["task_ids"])
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    observed: dict[str, Any] = {"run": 0, "finish": [], "cleanup": [], "body": 0}
    module = sys.modules["api.apps.restful_apis.agent"]
    actual_run, actual_finish, actual_cleanup = Canvas.run, module.finish_runtime, Canvas.cancel_task
    actual_stream, actual_call = StreamingResponse.stream_response, AgentStreamingResponse.__call__

    async def run(self: Canvas, **kwargs: Any) -> Any:
        observed["run"] += 1
        async with aclosing(actual_run(self, **kwargs)) as output:
            async for event in output:
                yield event

    def finish(task_id: str) -> Any:
        observed["finish"].append(task_id)
        return actual_finish(task_id)

    def cleanup(self: Canvas) -> bool:
        observed["cleanup"].append(self.task_id)
        return actual_cleanup(self)

    async def call(self: AgentStreamingResponse, *args: Any, **kwargs: Any) -> None:
        try:
            await actual_call(self, *args, **kwargs)
        finally:
            done.set()

    async def stream(self: StreamingResponse, send: Any) -> None:
        async def controlled_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                entered.set()
                try:
                    assert await asyncio.to_thread(release.wait, 20)
                    raise OSError("controlled debug response-start send failure")
                except BaseException as error:
                    observed["send_exception"] = type(error).__name__
                    raise
            observed["body"] += 1
            await send(message)

        await actual_stream(self, controlled_send)

    payload = {"agent_id": agent_id, "query": "header window", "stream": True}

    def request() -> Any:
        with HTTPSession() as client:
            return client.post(env["base"] + "/api/v1/agents/chat/completion", headers=env["client"].headers, json=payload, timeout=30)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
        patch.setattr(Canvas, "run", run)
        patch.setattr(Canvas, "cancel_task", cleanup)
        patch.setattr(module, "finish_runtime", finish)
        patch.setattr(StreamingResponse, "stream_response", stream)
        patch.setattr(AgentStreamingResponse, "__call__", call)
        connection = None
        future = None
        if failure == "send_error":
            future = pool.submit(request)
        else:
            address = urlsplit(env["base"])
            connection = socket.create_connection((address.hostname, address.port), timeout=10)
            body = json.dumps(payload).encode()
            headers = (
                "POST /api/v1/agents/chat/completion HTTP/1.1\r\n"
                f"Host: {address.netloc}\r\nAuthorization: {env['client'].headers['Authorization']}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            )
            connection.sendall(headers.encode() + body)
        try:
            assert entered.wait(10), "registered response did not reach response-start before its first iteration"
            task_ids = env["task_ids"][initial_tasks:]
            assert len(task_ids) == 1 and observed["run"] == observed["body"] == 0
            task_id = task_ids[0]
            active = read_binding(task_id)[1]
            assert active["state"] == "active" and active["version"] == 1
            assert active["principal_id"] == env["owners"][0] and active["resource_id"] == agent_id
            if cancelled:
                if cancel_base:
                    response = env["client"].post(cancel_base + f"/api/v1/tasks/{task_id}/cancel", headers={"Authorization": f"Bearer {env['keys'][0]}"}, timeout=30)
                    assert response.json()["code"] == 0
                else:
                    assert cancel(env, task_id).json()["retcode"] == 0
            nonce = REDIS_CONN.REDIS.get(f"{task_id}-cancel")
            assert bool(nonce) is cancelled
            if connection is not None:
                connection.shutdown(socket.SHUT_RDWR)
                connection.close()
            else:
                release.set()
                assert future.result(timeout=20).status_code == 500
            assert done.wait(5), "response-owned cleanup did not finish"
            assert observed["send_exception"] == ("OSError" if failure == "send_error" else "CancelledError")
            binding = read_binding(task_id)[1]
            assert binding["state"] == ("cancel_requested" if cancelled else "finished")
            assert {key: binding[key] for key in active if key != "state"} == {key: active[key] for key in active if key != "state"}
            assert observed["finish"] == observed["cleanup"] == [task_id]
            assert observed["run"] == observed["body"] == 0
            assert REDIS_CONN.REDIS.get(f"{task_id}-cancel") == (nonce if cancelled else "x")
            ttl = REDIS_CONN.REDIS.ttl(f"{task_id}-cancel")
            assert ttl > 86000 if cancelled else 3590 <= ttl <= 3600
            assert REDIS_CONN.REDIS.ttl(binding_key(task_id)) > 86000
            with Session(env["engine"]) as db:
                assert db.get(Task, task_id) is None
                assert db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.dialog_id == agent_id)) == 0
            assert read_state(env, agent_id) == before and REDIS_CONN.REDIS.get(replica_key) == replica
        finally:
            release.set()
            if connection is not None:
                connection.close()
    print(f"debug pre-iteration HTTP: {failure}, cancel={cancelled}; actual Canvas never started, lifecycle/nonce/TTL and PG/replica isolation independently verified")


@pytest.mark.parametrize("failure", ["send_error", "disconnect"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_registered_debug_response_start(cancel_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str, cancelled: bool) -> None:
    debug_start_case(cancel_api, monkeypatch, failure=failure, cancelled=cancelled)
