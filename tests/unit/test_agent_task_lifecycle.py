"""Detached webhook ownership and response-bound notification failure contracts."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks
from sqlalchemy.orm import Session
from starlette.requests import Request

from agent.canvas import Canvas
from api.apps.restful_apis import agent_api
from api.apps.services import agent_background_tasks
from api.db.db_models import UserCanvas


async def test_registry_keeps_tasks_and_observes_errors_and_cancellation(caplog: pytest.LogCaptureFixture) -> None:
    exited = asyncio.Event()

    async def wait() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    async def fail() -> None:
        raise ValueError("private data")

    task = agent_background_tasks.start_agent_task(wait())
    failed = agent_background_tasks.start_agent_task(fail())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert task in agent_background_tasks._tasks
    assert failed not in agent_background_tasks._tasks
    assert "ValueError" in caplog.text and "private data" not in caplog.text
    await agent_background_tasks.stop_agent_tasks()
    assert exited.is_set() and task.cancelled() and not agent_background_tasks._tasks


@pytest.mark.parametrize("cancel", [False, True])
async def test_invite_background_tasks_continue_after_failure_and_propagate_cancel(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, cancel: bool) -> None:
    tenant_api = sys.modules["api.apps.restful_apis.tenant"]
    calls: list[str] = []

    async def send(**kwargs: Any) -> None:
        calls.append(kwargs["to_email"])
        if len(calls) == 1:
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("private@example.com credential")

    monkeypatch.setattr(tenant_api, "send_invite_email", send)
    tasks = BackgroundTasks()
    tenant_api._schedule_invite_email(tasks, "tenant", "first@example.com", "owner")
    tenant_api._schedule_invite_email(tasks, "tenant", "second@example.com", "owner")
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await tasks()
        assert calls == ["first@example.com"]
    else:
        await tasks()
        assert calls == ["first@example.com", "second@example.com"]
    assert "private@example.com" not in caplog.text and "credential" not in caplog.text


@pytest.mark.parametrize("exit_kind", ["success", "error", "cancel"])
async def test_webhook_survives_request_db_close_and_closes_stream(monkeypatch: pytest.MonkeyPatch, exit_kind: str) -> None:
    started, proceed, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    dsl = {"components": {"begin": {"obj": {"component_name": "Begin", "params": {"mode": "Webhook", "execution_mode": "Immediately"}}}}}
    canvas_row = UserCanvas(id="agent", user_id="owner", dsl=dsl, canvas_category="agent_canvas")
    monkeypatch.setattr(agent_api.UserCanvasService, "get_by_id", lambda *_: canvas_row)
    monkeypatch.setattr(Canvas, "__init__", lambda *args, **kwargs: None)
    monkeypatch.setattr(Canvas, "__str__", lambda self: json.dumps({"executed": True}))

    async def run(self: Canvas, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        assert kwargs["user_id"] == "owner"
        started.set()
        try:
            await proceed.wait()
            if exit_kind == "error":
                raise RuntimeError("controlled")
            yield {"event": "done"}
        finally:
            closed.set()

    monkeypatch.setattr(Canvas, "run", run)
    writes = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    sessions_closed: list[bool] = []

    @asynccontextmanager
    async def task_db() -> AsyncIterator[Any]:
        try:
            yield writes
        finally:
            sessions_closed.append(True)

    monkeypatch.setattr(agent_api, "async_db_connection", task_db)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{}"}

    request = Request(
        {"type": "http", "method": "POST", "path": "/api/v1/agents/agent/webhook", "query_string": b"", "headers": [(b"content-type", b"application/json")], "client": ("127.0.0.1", 1)}, receive
    )
    with Session() as db:
        response = await agent_api.webhook("agent", request, db)
    assert response.status_code == 200
    await started.wait()
    if exit_kind == "cancel":
        await agent_background_tasks.stop_agent_tasks()
    else:
        task = next(iter(agent_background_tasks._tasks))
        proceed.set()
        await task
        await asyncio.sleep(0)
    assert closed.is_set() and not agent_background_tasks._tasks
    assert writes.execute.await_count == int(exit_kind == "success")
    assert writes.commit.await_count == int(exit_kind == "success")
    assert sessions_closed == ([True] if exit_kind == "success" else [])


async def test_app_shutdown_cancels_webhooks_before_workflow_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from api import apps, multirag_server
    from api.identity.mcp_delegation import runtime as delegation
    from api.identity.mcp_interactions import runtime as interactions
    from api.skills import core_runtime, protocol, runtime
    from common import app_config, config_utils, mcp_tool_call_conn

    disabled = SimpleNamespace(enabled=False)
    config = SimpleNamespace(identity=SimpleNamespace(mcp_issuer=disabled, mcp_delegation=disabled, mcp_interactions=disabled))
    monkeypatch.setattr(app_config, "get_app_config", lambda: config)
    monkeypatch.setattr(config_utils, "show_configs", lambda: None)
    monkeypatch.setattr(apps.settings, "print_rag_settings", lambda: None)
    monkeypatch.setattr(protocol, "selected_protocol", lambda: None)
    monkeypatch.setattr(apps.workflow_state_manager, "start", AsyncMock())
    monkeypatch.setattr(multirag_server, "stop_event", threading.Event())
    monkeypatch.setattr(threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    for module, names in [(runtime, ["start_skill_worker", "stop_skill_worker"]), (core_runtime, ["start_core_worker", "stop_core_worker"]), (interactions, ["stop_mcp_interactions"])]:
        for name in names:
            monkeypatch.setattr(module, name, AsyncMock())
    monkeypatch.setattr(interactions, "reset_mcp_interactions", lambda: None)
    monkeypatch.setattr(delegation, "reset_mcp_delegation_service", lambda: None)
    monkeypatch.setattr(mcp_tool_call_conn, "shutdown_all_mcp_sessions", lambda: None)
    started, closed = asyncio.Event(), asyncio.Event()

    async def work() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def shutdown() -> None:
        assert closed.is_set() and not agent_background_tasks._tasks

    monkeypatch.setattr(apps.workflow_state_manager, "shutdown", shutdown)
    async with apps.lifespan(apps.app):
        task = agent_background_tasks.start_agent_task(work())
        await started.wait()
    assert task.cancelled() and closed.is_set()
