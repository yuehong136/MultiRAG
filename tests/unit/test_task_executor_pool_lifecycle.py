"""Owned TOC execution must finish before a worker task releases its resources."""

import asyncio
import concurrent.futures
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from core.svr import task_executor


@dataclass
class Lifecycle:
    task: dict[str, Any]
    pools: list[concurrent.futures.ThreadPoolExecutor] = field(default_factory=list)
    inserted: list[list[dict[str, Any]]] = field(default_factory=list)
    counts: list[tuple[Any, ...]] = field(default_factory=list)
    progress: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)

    def assert_closed(self) -> None:
        for pool in self.pools:
            assert all(not thread.is_alive() for thread in pool._threads)
            with pytest.raises(RuntimeError, match="shutdown"):
                pool.submit(lambda: None)


@pytest.fixture
async def lifecycle(db: Session, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Lifecycle]:
    # Prime the shared executor before replacing only this module's pool factory.
    assert await asyncio.to_thread(lambda: "shared") == "shared"
    state = Lifecycle(
        {
            "id": "task",
            "task_type": "",
            "doc_id": "doc",
            "kb_id": "kb",
            "tenant_id": "tenant",
            "from_page": 0,
            "to_page": 1,
            "embd_id": "embed",
            "llm_id": "chat",
            "language": "English",
            "name": "file",
            "parser_id": "naive",
            "parser_config": {"toc_extraction": True},
        }
    )
    pool_type = concurrent.futures.ThreadPoolExecutor

    class OwnedPool(pool_type):
        def __init__(self) -> None:
            super().__init__(thread_name_prefix="toc-lifecycle-test")
            state.pools.append(self)

    monkeypatch.setattr(task_executor, "concurrent", SimpleNamespace(futures=SimpleNamespace(ThreadPoolExecutor=OwnedPool)))
    monkeypatch.setattr(task_executor, "has_canceled", lambda task_id: False)
    monkeypatch.setattr(task_executor, "set_progress", lambda *args, **kwargs: state.progress.append((args, kwargs)))
    monkeypatch.setattr(task_executor, "get_model_config_by_type_and_name", lambda *args: {})
    monkeypatch.setattr(task_executor, "LLMBundle", lambda *args, **kwargs: SimpleNamespace(encode=lambda texts: ([[0.0]], 0)))
    monkeypatch.setattr(
        task_executor.KnowledgebaseService, "get_by_id", lambda *args: SimpleNamespace(name="dataset", id="kb", parser_config={"raptor": {"use_raptor": True}, "graphrag": {"use_graphrag": True}})
    )
    monkeypatch.setattr(task_executor, "init_kb", AsyncMock())
    monkeypatch.setattr(task_executor, "build_chunks", AsyncMock(return_value=[{"pk": "chunk", "id": "chunk", "doc_id": "doc", "content_with_weight": "body"}]))
    monkeypatch.setattr(task_executor, "embedding", AsyncMock(return_value=7))
    monkeypatch.setattr(task_executor, "get_schema", AsyncMock(return_value={}))

    async def insert(*args: Any) -> bool:
        state.inserted.append(args[4])
        return True

    monkeypatch.setattr(task_executor, "insert_chunks", insert)
    monkeypatch.setattr(task_executor.DocumentService, "increment_chunk_num", lambda *args: state.counts.append(args[3:]))

    @contextmanager
    def connection() -> Iterator[Session]:
        yield db

    monkeypatch.setattr(task_executor, "db_connection", connection)

    async def toc(*args: Any) -> list[dict[str, Any]]:
        return [{"title": "section", "chunk_id": 0}]

    monkeypatch.setattr(task_executor, "run_toc_from_text", toc)
    yield state
    for pool in state.pools:
        pool.shutdown(wait=True, cancel_futures=True)
    assert await asyncio.to_thread(lambda: "still shared") == "still shared"


@pytest.mark.parametrize("exit_kind", ["canceled", "bind_error", "init_error", "parse_error", "empty", "embedding_error", "dataflow", "mindmap", "graphrag", "raptor_skip"])
async def test_pre_toc_exits_do_not_allocate_a_pool(db: Session, monkeypatch: pytest.MonkeyPatch, lifecycle: Lifecycle, exit_kind: str) -> None:
    fails = exit_kind.endswith("error")
    if exit_kind == "canceled":
        monkeypatch.setattr(task_executor, "has_canceled", lambda task_id: True)
    elif exit_kind == "bind_error":
        monkeypatch.setattr(task_executor, "LLMBundle", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bind")))
    elif exit_kind == "init_error":
        monkeypatch.setattr(task_executor, "init_kb", AsyncMock(side_effect=RuntimeError("init")))
    elif exit_kind == "parse_error":
        monkeypatch.setattr(task_executor, "build_chunks", AsyncMock(side_effect=RuntimeError("parse")))
    elif exit_kind == "empty":
        monkeypatch.setattr(task_executor, "build_chunks", AsyncMock(return_value=[]))
    elif exit_kind == "embedding_error":
        monkeypatch.setattr(task_executor, "embedding", AsyncMock(side_effect=RuntimeError("embed")))
    else:
        lifecycle.task["task_type"] = "raptor" if exit_kind == "raptor_skip" else exit_kind
        monkeypatch.setattr(task_executor, "run_dataflow", AsyncMock())
        monkeypatch.setattr(task_executor, "run_graphrag_for_kb", AsyncMock(return_value={}))
        monkeypatch.setattr(task_executor, "should_skip_raptor", lambda *args: True)
        monkeypatch.setattr(task_executor, "get_skip_reason", lambda *args: "structured")
    if fails:
        with pytest.raises(RuntimeError):
            await task_executor.do_handle_task(db, lifecycle.task)
    else:
        await task_executor.do_handle_task(db, lifecycle.task)
    assert lifecycle.pools == []
    assert lifecycle.inserted == []


@pytest.mark.parametrize("toc_enabled", [True, False])
async def test_real_toc_and_worker_completion_close_only_owned_pool(db: Session, lifecycle: Lifecycle, toc_enabled: bool) -> None:
    lifecycle.task["parser_config"]["toc_extraction"] = toc_enabled
    await task_executor.do_handle_task(db, lifecycle.task)
    assert len(lifecycle.pools) == int(toc_enabled)
    assert lifecycle.counts == ([(7, 1, 0), (0, 1, 0)] if toc_enabled else [(7, 1, 0)])
    assert len(lifecycle.inserted) == (2 if toc_enabled else 1)
    if toc_enabled:
        assert lifecycle.inserted[1][0]["toc_kwd"] == "toc"
        assert lifecycle.inserted[1][0]["available_int"] == 0
    assert lifecycle.progress[-1][1]["prog"] == 1.0
    lifecycle.assert_closed()


@pytest.mark.parametrize("exit_kind", ["schema_error", "insert_false", "insert_error", "count_error", "toc_error"])
async def test_post_submit_exits_join_threads_and_observe_errors(db: Session, monkeypatch: pytest.MonkeyPatch, lifecycle: Lifecycle, exit_kind: str, caplog: pytest.LogCaptureFixture) -> None:
    started = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    real_toc = task_executor.build_TOC

    def controlled_toc(*args: Any) -> Any:
        started.set()
        try:
            assert release.wait(5)
            if exit_kind in {"toc_error", "insert_false"}:
                raise RuntimeError("controlled TOC error")
            return real_toc(*args)
        finally:
            stopped.set()

    monkeypatch.setattr(task_executor, "build_TOC", controlled_toc)
    if exit_kind == "schema_error":
        monkeypatch.setattr(task_executor, "get_schema", AsyncMock(side_effect=RuntimeError("schema")))
    elif exit_kind == "insert_false":
        monkeypatch.setattr(task_executor, "insert_chunks", AsyncMock(return_value=False))
    elif exit_kind == "insert_error":
        monkeypatch.setattr(task_executor, "insert_chunks", AsyncMock(side_effect=RuntimeError("insert")))
    elif exit_kind == "count_error":
        monkeypatch.setattr(task_executor.DocumentService, "increment_chunk_num", lambda *args: (_ for _ in ()).throw(RuntimeError("count")))
    work = asyncio.create_task(task_executor.do_handle_task(db, lifecycle.task))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        # The loop must still run while the owned thread waits.
        await asyncio.sleep(0.01)
        assert not work.done()
        release.set()
        if exit_kind == "insert_false":
            await work
            assert "TOC generation failed during task cleanup" in caplog.text
        else:
            with pytest.raises(RuntimeError):
                await work
        assert stopped.is_set()
        lifecycle.assert_closed()
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.parametrize("cancel_at", ["schema", "toc_wait"])
async def test_repeated_cancellation_drains_work_before_return(db: Session, monkeypatch: pytest.MonkeyPatch, lifecycle: Lifecycle, cancel_at: str, caplog: pytest.LogCaptureFixture) -> None:
    started = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    entered = asyncio.Event()
    loop_errors: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: loop_errors.append(context))

    def toc(*args: Any) -> None:
        started.set()
        try:
            assert release.wait(5)
            raise RuntimeError("abandoned TOC error")
        finally:
            stopped.set()

    monkeypatch.setattr(task_executor, "build_TOC", toc)

    async def schema(*args: Any) -> dict[str, Any]:
        entered.set()
        if cancel_at == "schema":
            await asyncio.Event().wait()
        return {}

    monkeypatch.setattr(task_executor, "get_schema", schema)
    work = asyncio.create_task(task_executor.do_handle_task(db, lifecycle.task))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.sleep(0.01)
        work.cancel()
        await asyncio.sleep(0.01)
        work.cancel()
        await asyncio.sleep(0.01)
        assert not work.done()
        assert not stopped.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert stopped.is_set()
        lifecycle.assert_closed()
        await asyncio.sleep(0)
        assert loop_errors == []
        assert "abandoned TOC error" in caplog.text
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
        loop.set_exception_handler(old_handler)


async def test_business_cancellation_waits_for_toc_before_index_cleanup(db: Session, monkeypatch: pytest.MonkeyPatch, lifecycle: Lifecycle) -> None:
    release = threading.Event()
    started = threading.Event()
    stopped = threading.Event()
    canceled = False
    removed: list[str] = []

    def toc(*args: Any) -> None:
        started.set()
        try:
            assert release.wait(5)
        finally:
            stopped.set()

    async def schema(*args: Any) -> dict[str, Any]:
        nonlocal canceled
        assert await asyncio.to_thread(started.wait, 2)
        canceled = True
        return {}

    def delete(*args: Any) -> int:
        assert stopped.is_set()
        lifecycle.assert_closed()
        removed.append(args[1])
        return 1

    monkeypatch.setattr(task_executor, "build_TOC", toc)
    monkeypatch.setattr(task_executor, "get_schema", schema)
    monkeypatch.setattr(task_executor, "has_canceled", lambda task_id: canceled)
    monkeypatch.setattr(task_executor, "doc_store_exists", lambda *args: True)
    monkeypatch.setattr(task_executor, "delete_chunks_by_doc_id", delete)
    work = asyncio.create_task(task_executor.do_handle_task(db, lifecycle.task))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.sleep(0.01)
        assert not work.done()
        assert removed == []
        release.set()
        await work
        assert lifecycle.inserted == []
        assert removed == ["doc"]
        lifecycle.assert_closed()
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.parametrize("toc_failure", [False, True])
async def test_worker_ack_and_terminal_bookkeeping_follow_toc_shutdown(monkeypatch: pytest.MonkeyPatch, lifecycle: Lifecycle, toc_failure: bool) -> None:
    terminal: list[str] = []
    acknowledged: list[bool] = []
    logged: list[str] = []

    def finish(task_id: str) -> None:
        lifecycle.assert_closed()
        terminal.append(task_id)

    def ack() -> None:
        lifecycle.assert_closed()
        acknowledged.append(True)

    def log(*args: Any, **kwargs: Any) -> None:
        lifecycle.assert_closed()
        logged.append(kwargs["task_id"])

    if toc_failure:
        monkeypatch.setattr(task_executor, "run_toc_from_text", AsyncMock(side_effect=RuntimeError("worker TOC failure")))
    monkeypatch.setattr(task_executor, "collect", AsyncMock(return_value=(SimpleNamespace(ack=ack), lifecycle.task)))
    monkeypatch.setattr(task_executor, "finish_runtime", finish)
    monkeypatch.setattr(task_executor.PipelineOperationLogService, "record_pipeline_operation", log)
    monkeypatch.setattr(task_executor, "DONE_TASKS", 0)
    monkeypatch.setattr(task_executor, "FAILED_TASKS", 0)
    monkeypatch.setattr(task_executor, "CURRENT_TASKS", {})
    await task_executor.handle_task()
    assert terminal == ["task"]
    assert acknowledged == [True]
    assert logged == ["task"]
    assert task_executor.CURRENT_TASKS == {}
    assert task_executor.DONE_TASKS == int(not toc_failure)
    assert task_executor.FAILED_TASKS == int(toc_failure)
    lifecycle.assert_closed()
