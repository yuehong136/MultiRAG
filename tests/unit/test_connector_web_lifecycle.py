"""An exited npm leader cannot hide its still-listening child process."""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from tests.support.connector_web import stop_owned_web_group
from tests.support.resource_journal import ResourceJournal


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX session/process-group lifecycle")
@pytest.mark.parametrize("behavior", ["graceful", "ignore"])
def test_web_group_waits_for_descendant_after_leader_exits(tmp_path: Path, behavior: str) -> None:
    journal = ResourceJournal("connector-web-descendant-lifecycle")
    ready = tmp_path / "listener.json"
    child = """
import json,os,signal,socket,sys,time
listener=socket.socket()
listener.bind(('127.0.0.1',0))
listener.listen()
def stop(signum,frame):
    time.sleep(0.15)
    listener.close()
    raise SystemExit(0)
signal.signal(signal.SIGTERM,signal.SIG_IGN if sys.argv[2]=='ignore' else stop)
with open(sys.argv[1]+'.pending','x') as stream:
    json.dump({'pid':os.getpid(),'port':listener.getsockname()[1]},stream)
os.replace(sys.argv[1]+'.pending',sys.argv[1])
while True:
    time.sleep(0.05)
"""
    parent = "import subprocess,sys; child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]]); child.wait()"
    command = [sys.executable, "-B", "-c", parent, child, str(ready), behavior]
    journal.event("reserve-child-listener", address=["127.0.0.1", 0], ready_file=str(ready), pending_file=str(ready) + ".pending", behavior=behavior)
    journal.event("reserve-process-group", command=command, group="new session, pgid=child PID")
    process = subprocess.Popen(command, start_new_session=True)
    journal.event("process-group-started", pid=process.pid, group=process.pid)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline, "Owned child listener did not start"
            time.sleep(0.01)
        state = json.loads(ready.read_text())
        journal.event("child-listener-started", **state)
        with socket.socket() as listener:
            assert listener.connect_ex(("127.0.0.1", state["port"])) == 0
        # Stop only the leader first: this reproduces npm exiting before Vite.
        process.terminate()
        process.wait(timeout=2)
        assert psutil.Process(state["pid"]).is_running()
        with socket.socket() as listener:
            assert listener.connect_ex(("127.0.0.1", state["port"])) == 0
        escalated = stop_owned_web_group(process, port=state["port"], grace_seconds=2 if behavior == "graceful" else 0.2)
        assert escalated is (behavior == "ignore")
        with socket.socket() as listener:
            assert listener.connect_ex(("127.0.0.1", state["port"])) != 0
        try:
            assert psutil.Process(state["pid"]).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
        journal.event("web-group-independent-cleanup-readback", group=process.pid, child=state["pid"], live_processes_absent=True, listener_closed=True, kill_escalated=escalated)
    finally:
        stop_owned_web_group(process, grace_seconds=0.1)
        process.wait(timeout=1)
        ready.unlink(missing_ok=True)
        ready.with_suffix(".json.pending").unlink(missing_ok=True)
