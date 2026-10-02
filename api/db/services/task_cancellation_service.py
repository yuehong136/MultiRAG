"""Authorized cancellation for persisted tasks and registered Canvas attempts."""

import asyncio
from datetime import datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Document, Knowledgebase, Task, UserCanvas, UserTenant
from common.constants import TaskStatus
from core.utils.task_runtime import (
    DELETE_CANCEL_SCRIPT,
    TASK_CANCEL_MARKER,
    TASK_RUNTIME_TTL,
    TaskBinding,
    read_binding,
    register_runtime,
    request_runtime_cancel,
    restore_runtime,
    runtime_redis,
)


async def require_membership(db: AsyncSession, tenant_id: str, principal_id: str) -> None:
    member = await db.scalar(
        select(UserTenant.id).where(
            UserTenant.tenant_id == tenant_id,
            UserTenant.user_id == principal_id,
            UserTenant.status == "1",
            UserTenant.role.in_(["owner", "normal", "admin"]),
        )
    )
    if member is None:
        raise PermissionError("No authorization to cancel this task.")


async def require_canvas(db: AsyncSession, resource_id: str, tenant_id: str, principal_id: str) -> None:
    canvas = await db.scalar(select(UserCanvas).where(UserCanvas.id == resource_id))
    if canvas is None or canvas.user_id != tenant_id:
        raise PermissionError("Task resource is unavailable.")
    if principal_id != tenant_id:
        if canvas.permission != "team":
            raise PermissionError("No authorization to cancel this task.")
        await require_membership(db, tenant_id, principal_id)


async def bind_canvas_task(db: AsyncSession, task_id: str, principal_id: str, resource_id: str, kind: Literal["agent", "dataflow"] = "agent") -> None:
    # Read the resource owner from SQL, never from DSL/runtime user_id.
    tenant_id = await db.scalar(select(UserCanvas.user_id).where(UserCanvas.id == resource_id))
    if tenant_id is None:
        raise PermissionError("Task resource is unavailable.")
    await require_canvas(db, resource_id, tenant_id, principal_id)
    await asyncio.to_thread(register_runtime, task_id, principal_id, tenant_id, resource_id, kind)


async def authorize_binding(db: AsyncSession, binding: TaskBinding, principal_id: str) -> None:
    await require_canvas(db, binding["resource_id"], binding["tenant_id"], principal_id)


async def cancel_task(db: AsyncSession, task_id: str, principal_id: str) -> None:
    """Commit a cancel request; success does not assert that a worker has stopped.

    Missing/expired unbound IDs and terminal tasks are side-effect-free no-ops.
    SQL tasks serialize on their row. Runtime-only attempts use Redis CAS.
    Redis is written before SQL commit; commit failure attempts nonce-checked
    compensation, and always reports an error, including uncertain rollback.
    """
    token = uuid4().hex
    flag_written = False
    previous_binding: str | None = None
    try:
        task_document_id = await db.scalar(select(Task.doc_id).where(Task.id == task_id))
        # Document producers/ingest use Doc -> Task. Avoid the inverse lock
        # order when Task REST cancellation targets a real document.
        if task_document_id and task_document_id not in {"dataflow_x", "graph_raptor_x"}:
            await db.scalar(select(Document).where(Document.id == task_document_id).with_for_update())
        task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True))
        runtime = await asyncio.to_thread(read_binding, task_id)
        document: Document | None = None
        if task is None:
            if runtime is None:
                await db.rollback()
                return
            await authorize_binding(db, runtime[1], principal_id)
        elif task.doc_id == "dataflow_x":
            if runtime is None or runtime[1]["kind"] != "dataflow":
                raise PermissionError("Task ownership is unavailable.")
            await authorize_binding(db, runtime[1], principal_id)
        else:
            if task.doc_id == "graph_raptor_x":
                kb = await db.scalar(
                    select(Knowledgebase).where(
                        Knowledgebase.status == "1",
                        or_(Knowledgebase.graphrag_task_id == task_id, Knowledgebase.raptor_task_id == task_id, Knowledgebase.mindmap_task_id == task_id),
                    )
                )
            else:
                document = await db.scalar(select(Document).where(Document.id == task.doc_id, Document.status == "1").with_for_update())
                kb = await db.scalar(select(Knowledgebase).where(Knowledgebase.id == document.kb_id, Knowledgebase.status == "1")) if document else None
            if kb is None:
                raise PermissionError("Task resource is unavailable.")
            await require_membership(db, kb.tenant_id, principal_id)

        # Authorize even completed/failed tasks, without touching their flags.
        if (task is not None and (task.progress < 0 or task.progress >= 1)) or (runtime is not None and runtime[1]["state"] != "active"):
            await db.rollback()
            return

        if runtime is not None:
            previous_binding = runtime[0]
            flag_written = await asyncio.to_thread(request_runtime_cancel, task_id, previous_binding, token)
            if not flag_written:
                # A finish, expiry or another cancellation won the CAS. Re-read
                # through the same authorization path before acknowledging.
                current = await asyncio.to_thread(read_binding, task_id)
                if current is not None:
                    await authorize_binding(db, current[1], principal_id)
                    if current[1]["state"] == "active":
                        raise RuntimeError("Task ownership changed; retry cancellation.")
                await db.rollback()
                return
        else:
            flag_written = bool(await asyncio.to_thread(runtime_redis().set, f"{task_id}-cancel", token, ex=TASK_RUNTIME_TTL))
            if not flag_written:
                raise ConnectionError("Failed to submit task cancellation to Redis.")

        if task is not None:
            message = f"\n{datetime.now().strftime('%H:%M:%S')} {TASK_CANCEL_MARKER} Task stopped by user."
            await db.execute(update(Task).where(Task.id == task_id, Task.progress >= 0, Task.progress < 1).values(progress=-1, progress_msg=func.coalesce(Task.progress_msg, "") + message))
            if document is not None:
                await db.execute(
                    update(Document)
                    .where(Document.id == document.id, Document.run.in_([TaskStatus.RUNNING.value, TaskStatus.SCHEDULE.value]))
                    .values(run=TaskStatus.CANCEL.value, progress=0, progress_msg=func.coalesce(Document.progress_msg, "") + message)
                )
        await db.commit()
    except BaseException as error:
        rollback_failure: Exception | None = None
        try:
            await db.rollback()
        except Exception as rollback_error:
            rollback_failure = rollback_error
        if flag_written:
            try:
                restored = (
                    await asyncio.to_thread(restore_runtime, task_id, previous_binding, token)
                    if previous_binding is not None
                    else bool(await asyncio.to_thread(runtime_redis().eval, DELETE_CANCEL_SCRIPT, 1, f"{task_id}-cancel", token))
                )
                if not restored:
                    raise RuntimeError("Cancellation failed; Redis compensation could not be confirmed.") from error
            except Exception as compensation_error:
                raise RuntimeError("Cancellation failed; Redis compensation could not be confirmed.") from compensation_error
        if rollback_failure is not None:
            raise RuntimeError("Cancellation failed; SQL rollback could not be confirmed.") from rollback_failure
        raise
