"""Cancellation authorization, side effects and cross-store failures."""

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.services import task_cancellation_service as service


class CancellationSession(AsyncSession):
    def __init__(self, values: list[Any], fail_commit: bool = False) -> None:
        super().__init__()
        self.values = iter(values)
        self.fail_commit = fail_commit
        self.updates: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        return next(self.values)

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        self.updates.append(statement)
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("SQL commit failed")
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class CancelRedis:
    def __init__(self) -> None:
        self.writes: list[tuple[Any, ...]] = []
        self.result = True
        self.failure = False
        self.compensation = 1

    def set(self, *args: Any, **kwargs: Any) -> bool:
        if self.failure:
            raise ConnectionError("Redis unavailable")
        self.writes.append(args)
        return self.result

    def eval(self, *args: Any, **kwargs: Any) -> int:
        self.writes.append(args)
        return self.compensation


@pytest.fixture
def cancel_redis(monkeypatch: pytest.MonkeyPatch) -> CancelRedis:
    redis = CancelRedis()
    monkeypatch.setattr(service, "runtime_redis", lambda: redis)
    monkeypatch.setattr(service, "read_binding", lambda task_id: None)
    return redis


def doc_task(progress: float = 0.2) -> SimpleNamespace:
    return SimpleNamespace(id="task", doc_id="document", progress=progress, progress_msg=None)


def doc_access(task: SimpleNamespace) -> list[Any]:
    return [task, SimpleNamespace(id="document", kb_id="dataset"), SimpleNamespace(tenant_id="owner"), "membership"]


async def test_unknown_is_noop(cancel_redis: CancelRedis) -> None:
    db = CancellationSession([None])
    await service.cancel_task(db, "missing", "owner")
    assert not cancel_redis.writes and not db.updates and not db.commits


@pytest.mark.parametrize("progress", [-1, 1, 1.5])
async def test_terminal_authorized_noop(cancel_redis: CancelRedis, progress: float) -> None:
    db = CancellationSession(doc_access(doc_task(progress)))
    await service.cancel_task(db, "task", "owner")
    assert not cancel_redis.writes and not db.updates


@pytest.mark.parametrize("missing", ["document", "dataset", "membership"])
async def test_foreign_or_orphan_never_writes(cancel_redis: CancelRedis, missing: str) -> None:
    values = doc_access(doc_task())
    values[{"document": 1, "dataset": 2, "membership": 3}[missing]] = None
    db = CancellationSession(values)
    with pytest.raises(PermissionError):
        await service.cancel_task(db, "task", "foreign")
    assert not cancel_redis.writes and not db.updates and not db.commits


async def test_nullable_logs_and_task_doc_updates(cancel_redis: CancelRedis) -> None:
    db = CancellationSession(doc_access(doc_task()))
    await service.cancel_task(db, "task", "owner")
    assert len(cancel_redis.writes) == 1 and len(db.updates) == 2 and db.commits == 1
    assert all("coalesce" in str(statement).lower() for statement in db.updates)


@pytest.mark.parametrize("mode", ["false", "exception"])
async def test_redis_failure_never_commits(cancel_redis: CancelRedis, mode: str) -> None:
    cancel_redis.result = mode != "false"
    cancel_redis.failure = mode == "exception"
    db = CancellationSession(doc_access(doc_task()))
    with pytest.raises(ConnectionError):
        await service.cancel_task(db, "task", "owner")
    assert not db.updates and not db.commits and db.rollbacks == 1


@pytest.mark.parametrize("compensation", [0, 1])
async def test_sql_failure_reports_error_and_compensates_only_own_nonce(cancel_redis: CancelRedis, compensation: int) -> None:
    cancel_redis.compensation = compensation
    db = CancellationSession(doc_access(doc_task()), fail_commit=True)
    with pytest.raises(RuntimeError, match="SQL commit failed" if compensation else "compensation could not be confirmed"):
        await service.cancel_task(db, "task", "owner")
    assert len(cancel_redis.writes) == 2
    assert cancel_redis.writes[0][1] == cancel_redis.writes[1][-1]
    assert not db.commits


async def test_rollback_failure_still_compensates_redis(cancel_redis: CancelRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    db = CancellationSession(doc_access(doc_task()), fail_commit=True)

    async def fail_rollback() -> None:
        raise ConnectionError("SQL rollback failed")

    monkeypatch.setattr(db, "rollback", fail_rollback)
    with pytest.raises(RuntimeError, match="SQL rollback could not be confirmed"):
        await service.cancel_task(db, "task", "owner")
    assert len(cancel_redis.writes) == 2 and cancel_redis.writes[0][1] == cancel_redis.writes[1][-1]


async def test_unbound_dataflow_is_denied(cancel_redis: CancelRedis) -> None:
    db = CancellationSession([SimpleNamespace(doc_id="dataflow_x", progress=0)])
    with pytest.raises(PermissionError, match="ownership"):
        await service.cancel_task(db, "task", "owner")
    assert not cancel_redis.writes and not db.updates


async def test_binding_failure_is_not_silenced(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(*args: Any) -> None:
        raise ConnectionError("binding Redis failed")

    monkeypatch.setattr(service, "register_runtime", reject)
    db = CancellationSession(["owner", SimpleNamespace(user_id="owner", permission="me")])
    with pytest.raises(ConnectionError, match="binding Redis failed"):
        await service.bind_canvas_task(db, "task", "owner", "canvas")


@pytest.mark.parametrize("kind", ["agent", "dataflow"])
@pytest.mark.parametrize("state", ["finished", "cancel_requested"])
async def test_bound_terminal_does_not_write(cancel_redis: CancelRedis, monkeypatch: pytest.MonkeyPatch, kind: str, state: str) -> None:
    binding = {"version": 1, "principal_id": "owner", "tenant_id": "owner", "resource_id": "canvas", "kind": kind, "state": state}
    monkeypatch.setattr(service, "read_binding", lambda task_id: ("trusted", binding))
    db = CancellationSession([None, SimpleNamespace(user_id="owner", permission="me")])
    await service.cancel_task(db, "task", "owner")
    assert not cancel_redis.writes and not db.updates


async def test_finish_wins_runtime_cas(cancel_redis: CancelRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    binding = {"version": 1, "principal_id": "owner", "tenant_id": "owner", "resource_id": "canvas", "kind": "agent", "state": "active"}
    reads = iter([("before", binding), ("after", {**binding, "state": "finished"})])
    monkeypatch.setattr(service, "read_binding", lambda task_id: next(reads))
    monkeypatch.setattr(service, "request_runtime_cancel", lambda *args: False)
    db = CancellationSession([None, SimpleNamespace(user_id="owner", permission="me"), SimpleNamespace(user_id="owner", permission="me")])
    await service.cancel_task(db, "task", "owner")
    assert not cancel_redis.writes and not db.updates
