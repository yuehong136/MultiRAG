"""Synchronous integration drivers own their loops despite Agent loop patches."""

import json
import os
import subprocess
import sys

import pytest

_PROBE = """
import asyncio
import json
import sys

from litellm.litellm_core_utils.logging_worker import LoggingWorker
from tests.support.event_loop import run

scenario, patched = sys.argv[1:]
external_loop = asyncio.new_event_loop()
asyncio.set_event_loop(external_loop)
if patched == 'yes':
    import nest_asyncio
    nest_asyncio.apply(external_loop)

foreign_worker = LoggingWorker()
foreign_started = asyncio.Event()

async def foreign_log() -> None:
    foreign_started.set()
    await asyncio.Event().wait()

async def start_foreign() -> None:
    foreign_worker.start()
    foreign_worker.enqueue(foreign_log())
    await foreign_started.wait()

external_loop.run_until_complete(start_foreign())
foreign_task = foreign_worker._worker_task
foreign_queue = foreign_worker._queue
foreign_running = set(foreign_worker._running_tasks)
owned_worker = LoggingWorker()
owned_log_started = asyncio.Event()
owned_loops = []
owned_children = []
settled = []

async def owned_log() -> None:
    owned_log_started.set()
    try:
        await asyncio.Event().wait()
    finally:
        settled.append('logging')

async def owned_child(started: asyncio.Event) -> None:
    started.set()
    try:
        await asyncio.Event().wait()
    finally:
        settled.append('child')

async def operation() -> int:
    owned_loops.append(asyncio.get_running_loop())
    owned_worker.start()
    owned_worker.enqueue(owned_log())
    started = asyncio.Event()
    owned_children.append(asyncio.create_task(owned_child(started)))
    await started.wait()
    # The real logging task must also be running before the driver exits.
    await owned_log_started.wait()
    if scenario == 'error':
        raise LookupError('controlled operation failure')
    if scenario == 'cancel':
        raise asyncio.CancelledError
    return 42

async def stop_foreign() -> None:
    await foreign_worker.stop()
    await foreign_worker.clear_queue()

async def stop_owned() -> None:
    await owned_worker.stop()
    await owned_worker.clear_queue()
    for child in owned_children:
        child.cancel()
    await asyncio.gather(*owned_children, return_exceptions=True)

try:
    try:
        value = run(operation())
    except LookupError:
        assert scenario == 'error'
    except asyncio.CancelledError:
        assert scenario == 'cancel'
    else:
        assert scenario == 'success' and value == 42
    assert len(owned_loops) == 1
    owned_loop = owned_loops[0]
    assert owned_loop is not external_loop and owned_loop.is_closed()
    assert all(task.done() and task.cancelled() for task in owned_children)
    assert owned_worker._worker_task.done()
    assert not owned_worker._running_tasks
    assert owned_worker._queue.empty()
    assert sorted(settled) == ['child', 'logging']
    assert not asyncio.all_tasks(owned_loop)
    assert asyncio.get_event_loop() is external_loop
    assert not external_loop.is_closed()
    assert foreign_worker._worker_task is foreign_task and not foreign_task.done()
    assert foreign_worker._queue is foreign_queue
    assert foreign_worker._running_tasks == foreign_running
    assert all(not task.done() for task in foreign_running)
    print(json.dumps({'owned_loop_closed': True, 'owned_tasks_drained': True, 'foreign_worker_preserved': True}))
finally:
    if owned_loops and not owned_loops[0].is_closed():
        owned_loops[0].run_until_complete(stop_owned())
    external_loop.run_until_complete(stop_foreign())
    assert not asyncio.all_tasks(external_loop)
    external_loop.close()
    asyncio.set_event_loop(None)
"""


@pytest.mark.parametrize("scenario", ["success", "error", "cancel"])
@pytest.mark.parametrize("patched", [False, True], ids=["native", "nest_asyncio"])
def test_driver_drains_only_its_owned_loop(scenario: str, patched: bool) -> None:
    env = {**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    result = subprocess.run([sys.executable, "-B", "-c", _PROBE, scenario, "yes" if patched else "no"], capture_output=True, text=True, env=env, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"owned_loop_closed": True, "owned_tasks_drained": True, "foreign_worker_preserved": True}
