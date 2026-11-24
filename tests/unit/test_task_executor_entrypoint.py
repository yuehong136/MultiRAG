"""Task executor 进程入口的异常处理回归测试。"""

import asyncio
import logging

import pytest

from core.svr import task_executor


def test_run_task_executor_logs_unhandled_exception_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing_main() -> None:
        raise RuntimeError("synthetic task executor failure")

    def disable_faulthandler() -> None:
        pass

    def skip_logger_initialization(_consumer_name: str) -> None:
        pass

    monkeypatch.setattr(task_executor, "main", failing_main)
    monkeypatch.setattr(task_executor.faulthandler, "enable", disable_faulthandler)
    monkeypatch.setattr(task_executor, "init_root_logger", skip_logger_initialization)

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc_info:
        task_executor.run_task_executor([])

    assert exc_info.value.code == 1
    assert "Task executor terminated unexpectedly" in caplog.text
    assert "synthetic task executor failure" in caplog.text


def test_run_task_executor_runs_main_on_a_loop_async_psycopg_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Async psycopg needs add_reader/add_writer; win32 asyncio.run() would default to a Proactor loop.
    seen: list[asyncio.AbstractEventLoop] = []

    async def recording_main() -> None:
        seen.append(asyncio.get_running_loop())

    def disable_faulthandler() -> None:
        pass

    def skip_logger_initialization(_consumer_name: str) -> None:
        pass

    monkeypatch.setattr(task_executor, "main", recording_main)
    monkeypatch.setattr(task_executor.faulthandler, "enable", disable_faulthandler)
    monkeypatch.setattr(task_executor, "init_root_logger", skip_logger_initialization)

    task_executor.run_task_executor([])

    assert len(seen) == 1
    assert isinstance(seen[0], asyncio.SelectorEventLoop)
