"""Real authenticated HTTP cancellation, scratch PostgreSQL and owned Redis.

The Agent wait is a controllable test component boundary; Canvas, Begin,
VariableAssigner, Message, sessions, authorization and storage are real.
"""

import asyncio
import threading
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn  # noqa: F401 -- load the runner before legacy component nest_asyncio patches
from sqlalchemy.orm import Session

from agent.component.variable_assigner import VariableAssigner
from api.db.db_models import API4Conversation, Document, File, File2Document, Knowledgebase, Task, UserCanvas, UserCanvasVersion
from api.db.services.task_service import queue_tasks
from common import settings
from common.constants import TaskStatus
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_update_release import message_dsl
from tests.support.agent_update_release import release_api as release_api


@pytest.fixture
def cancel_api(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = release_api
    queue = f"cancel-test:{uuid4().hex}"
    monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: queue)
    kb_id, doc_id, file_id = uuid4().hex, uuid4().hex, uuid4().hex
    owner = env["owners"][0]
    try:
        with Session(env["engine"]) as db:
            db.add(Knowledgebase(id=kb_id, tenant_id=owner, created_by=owner, name="Cancellation scratch", embd_id="", permission="team"))
            db.add(Document(id=doc_id, kb_id=kb_id, parser_id="naive", type="doc", created_by=owner, name="cancel.txt", location="cancel.txt", run=TaskStatus.RUNNING.value))
            db.add(File(id=file_id, parent_id=owner, tenant_id=owner, created_by=owner, name="cancel.txt", location="cancel.txt", type="doc"))
            db.add(File2Document(id=uuid4().hex, file_id=file_id, document_id=doc_id))
            db.commit()
            doc = db.get(Document, doc_id)
            assert doc is not None
            queue_tasks(db, doc.to_dict(), "unused", "cancel.txt", 0)
            task_id = db.scalar(sa.select(Task.id).where(Task.doc_id == doc_id))
            assert task_id
        env["task_ids"].append(task_id)
        env.update(kb_id=kb_id, doc_id=doc_id, document_task_id=task_id, queue=queue)
        yield env
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(Task).where(sa.or_(Task.id.in_(env["task_ids"]), Task.doc_id == doc_id)))
            db.execute(sa.delete(Document).where(Document.id == doc_id))
            db.execute(sa.delete(File2Document).where(File2Document.document_id == doc_id))
            db.execute(sa.delete(File).where(File.id == file_id))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id == kb_id))
            db.commit()
            assert db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.id.in_(env["task_ids"]))) == 0
            assert db.get(Document, doc_id) is None and db.get(Knowledgebase, kb_id) is None
            assert db.get(File, file_id) is None
        redis = REDIS_CONN.REDIS
        redis.delete(queue)
        assert not redis.exists(queue)
        print("task acceptance cleanup: Task/Document/KB rows and owned queue removed; parent verifies auth, Canvas, sessions, keys and listener")


def cancel(env: dict[str, Any], task_id: str, *, method: str = "post", principal: int = 0, api_key: bool = False) -> Any:
    path = f"/api/v1/tasks/{task_id}/cancel" if method == "post" else f"/api/v1/tasks/{task_id}"
    return env["client"].request(
        method,
        env["base"] + path,
        json={"action": "stop"} if method == "patch" else None,
        headers={"Authorization": f"Bearer {env['keys'][principal] if api_key else env['jwts'][principal]}"},
        timeout=30,
    )


def persistent_state(env: dict[str, Any], task_id: str) -> tuple[float, str | None, str, float, str | None]:
    with Session(env["engine"]) as db:
        task = db.get(Task, task_id)
        doc = db.get(Document, env["doc_id"])
        assert task is not None and doc is not None
        return task.progress, task.progress_msg, doc.run, doc.progress, doc.progress_msg


def wait_dsl() -> dict[str, Any]:
    dsl = message_dsl("completed")
    dsl["components"]["begin"]["downstream"] = ["VariableAssigner:wait"]
    dsl["components"]["VariableAssigner:wait"] = {
        "obj": {"component_name": "VariableAssigner", "params": {"variables": []}},
        "upstream": ["begin"],
        "downstream": ["Message:answer"],
    }
    dsl["components"]["Message:answer"]["upstream"] = ["VariableAssigner:wait"]
    return dsl


@pytest.fixture
def wait_gate(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    gate: dict[str, Any] = {"enabled": False, "entered": {}, "release": {}}

    async def controlled_wait(self: VariableAssigner, **kwargs: Any) -> None:
        task_id = self._canvas.task_id
        if gate["enabled"]:
            release = gate["release"].setdefault(task_id, threading.Event())
            gate["entered"].setdefault(task_id, threading.Event()).set()
            if not await asyncio.to_thread(release.wait, 20):
                raise RuntimeError("Test wait deadline exceeded")
        self._invoke(**kwargs)

    monkeypatch.setattr(VariableAssigner, "_invoke_async", controlled_wait, raising=False)
    yield gate
    for event in gate["release"].values():
        event.set()


def retirement_state(env: dict[str, Any]) -> tuple[dict[str, Any], dict[str, tuple[bytes, int]]]:
    """Read full owned SQL rows and Redis payloads independently of HTTP."""
    with Session(env["engine"]) as db:
        canvas_ids = list(db.scalars(sa.select(UserCanvas.id).where(UserCanvas.user_id.in_(env["owners"]))))
        session_ids = list(db.scalars(sa.select(API4Conversation.id).where(API4Conversation.dialog_id.in_(canvas_ids))))
        scoped = [
            (Task, Task.id.in_(env["task_ids"])),
            (Document, Document.id == env["doc_id"]),
            (Knowledgebase, Knowledgebase.id == env["kb_id"]),
            (UserCanvas, UserCanvas.id.in_(canvas_ids)),
            (UserCanvasVersion, UserCanvasVersion.user_canvas_id.in_(canvas_ids)),
            (API4Conversation, API4Conversation.id.in_(session_ids)),
        ]
        sql = {model.__name__: [dict(row) for row in db.execute(sa.select(model.__table__).where(condition).order_by(*model.__table__.primary_key)).mappings()] for model, condition in scoped}
    redis = REDIS_CONN.REDIS
    keys = {env["queue"]}
    for identifier in [*env["owners"], *env["task_ids"], *canvas_ids, *session_ids]:
        keys.update(redis.scan_iter(match=f"*{identifier}*"))
    # DUMP includes the complete payload for strings, streams and hashes, but
    # excludes expiry. Track natural TTL decay separately from byte equality.
    payloads = {key: (redis.dump(key), redis.pttl(key)) for key in sorted(keys) if redis.exists(key)}
    return sql, payloads
