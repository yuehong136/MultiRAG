"""Execution budgets and failure/cancellation contracts across retry phases."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from api.utils.api_utils import get_parser_config
from api.utils.validation_utils import GraphragConfig
from common.exceptions import TaskCanceledException
from common.graphrag_config import GraphRAGExecutionConfig
from core.graphrag import persistence
from core.graphrag.general import index
from core.graphrag.light.graph_extractor import GraphExtractor


@pytest.mark.parametrize("model", [GraphRAGExecutionConfig, GraphragConfig])
@pytest.mark.parametrize("value", [None, True, "2", 1.5, float("nan"), float("inf"), -1])
def test_attempt_numbers_are_strict(model: type[GraphRAGExecutionConfig], value: Any) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"retry_attempts": value})


@pytest.mark.parametrize("field", ["retry_backoff_seconds", "retry_backoff_max_seconds"])
@pytest.mark.parametrize("value", [None, True, "0", float("nan"), float("inf"), -1])
def test_backoff_numbers_are_finite(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        GraphragConfig.model_validate({field: value})


def test_api_defaults_and_zero_budgets_roundtrip() -> None:
    defaults = GraphRAGExecutionConfig().model_dump()
    api = get_parser_config("naive", None)["graphrag"]
    assert all(api[key] == value for key, value in defaults.items())
    explicit = dict.fromkeys(
        ["merge_timeout_seconds", "resolution_timeout_seconds", "community_timeout_seconds", "lock_acquire_timeout_seconds", "retry_backoff_seconds", "retry_backoff_max_seconds"], 0
    )
    config = GraphragConfig.model_validate(explicit)
    assert all(config.model_dump()[key] == 0 for key in explicit)
    assert config.lock_wait_seconds == 600
    assert GraphRAGExecutionConfig.model_validate(config.model_dump()).model_dump() == config.model_dump(include=set(defaults))
    with pytest.raises(ValidationError):
        GraphragConfig(unknown=1)


async def retry(operation: Any, **kwargs: Any) -> Any:
    return await index._run_with_retry(
        "test", operation, attempts=kwargs.pop("attempts", 2), timeout_seconds=kwargs.pop("timeout_seconds", 0), backoff_seconds=kwargs.pop("backoff_seconds", 0), backoff_max_seconds=10, **kwargs
    )


async def test_timeout_cleans_attempt_before_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(index, "has_canceled", lambda _: False)
    calls = 0
    active = 0
    finished: list[int] = []

    async def operation() -> str:
        nonlocal calls, active
        calls += 1
        number = calls
        assert active == 0
        active += 1
        try:
            if number == 1:
                await asyncio.Event().wait()
            return "ok"
        finally:
            await asyncio.sleep(0.01)
            active -= 1
            finished.append(number)

    assert await retry(operation, timeout_seconds=0.03) == "ok"
    assert calls == 2 and active == 0 and finished == [1, 2]


@pytest.mark.parametrize("cancel_error", [TaskCanceledException, asyncio.CancelledError])
async def test_cancellation_never_retries(cancel_error: type[BaseException]) -> None:
    operation = AsyncMock(side_effect=cancel_error("cancel"))
    with pytest.raises(cancel_error):
        await retry(operation)
    assert operation.await_count == 1


async def test_task_cancel_interrupts_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    canceled = False
    monkeypatch.setattr(index, "has_canceled", lambda _: canceled)
    started = asyncio.Event()

    async def fail() -> None:
        started.set()
        raise RuntimeError("transient")

    operation = AsyncMock(side_effect=fail)
    running = asyncio.create_task(retry(operation, backoff_seconds=10, task_id="task"))
    await started.wait()
    canceled = True
    with pytest.raises(TaskCanceledException):
        await asyncio.wait_for(running, 1)
    assert operation.await_count == 1


async def test_active_task_cancel_drains_child(monkeypatch: pytest.MonkeyPatch) -> None:
    canceled, stopped = False, False
    monkeypatch.setattr(index, "has_canceled", lambda _: canceled)
    started = asyncio.Event()

    async def operation() -> None:
        nonlocal stopped
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped = True

    running = asyncio.create_task(retry(operation, task_id="task"))
    await started.wait()
    canceled = True
    with pytest.raises(TaskCanceledException):
        await asyncio.wait_for(running, 1)
    assert stopped


async def test_lease_wait_is_bounded_and_cancellable(monkeypatch: pytest.MonkeyPatch) -> None:
    class Lock:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def acquire(self, *, blocking: bool) -> bool:
            assert blocking is False
            return False

        def owned(self) -> bool:
            return False

    monkeypatch.setattr(persistence, "Lock", Lock)
    monkeypatch.setattr(persistence, "has_canceled", lambda _: False)
    with pytest.raises(TimeoutError):
        async with persistence.GraphRAGLease("tenant", "kb", wait=0.01):
            pytest.fail("lock was never acquired")
    token = persistence.current_task.set("task")
    monkeypatch.setattr(persistence, "has_canceled", lambda _: True)
    try:
        with pytest.raises(TaskCanceledException):
            async with persistence.GraphRAGLease("tenant", "kb", wait=100):
                pytest.fail("canceled task acquired lock")
    finally:
        persistence.current_task.reset(token)


@pytest.mark.parametrize("error", [RuntimeError("provider"), TaskCanceledException("cancel")])
async def test_partial_extraction_cannot_succeed(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    extractor = GraphExtractor.__new__(GraphExtractor)
    monkeypatch.setattr(extractor, "_process_single_content", AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        await extractor("doc", ["one", "two"])


async def test_invalid_runtime_budget_rejected_before_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    lease = AsyncMock()
    monkeypatch.setattr(index, "GraphRAGLease", lease)
    with pytest.raises(ValidationError):
        await index.run_graphrag_for_kb({"id": "t", "kb_id": "k", "tenant_id": "u"}, [], "Chinese", {"graphrag": {"merge_timeout_seconds": float("nan")}}, None, None, lambda **_: None)
    lease.assert_not_called()


@pytest.mark.parametrize(
    "field",
    [
        "build_subgraph_retry_attempts",
        "merge_retry_attempts",
        "resolution_retry_attempts",
        "community_retry_attempts",
        "build_subgraph_timeout_per_chunk_seconds",
        "build_subgraph_min_timeout_seconds",
        "merge_timeout_seconds",
        "resolution_timeout_seconds",
        "community_timeout_seconds",
        "lock_acquire_timeout_seconds",
    ],
)
@pytest.mark.parametrize("value", [None, True, "2", 1.5, float("nan"), float("inf")])
def test_each_execution_integer_budget_rejects_ambiguous_values(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        GraphragConfig.model_validate({field: value})
    with pytest.raises(ValidationError):
        GraphRAGExecutionConfig.model_validate({field: value})
