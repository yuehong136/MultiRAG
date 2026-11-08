"""The Go launcher starts both built binaries and reaps its own sibling."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.support.resource_journal import ResourceJournal


@pytest.mark.parametrize("first", ["admin", "api"])
def test_go_launcher_returns_child_failure_and_stops_sibling(tmp_path: Path, first: str) -> None:
    journal = ResourceJournal("go-build-launcher")
    binaries: dict[str, Path] = {}
    for name in ("admin", "api"):
        path = tmp_path / name
        binaries[name] = path
        journal.event("reserve-file", path=str(path), purpose="controlled Go launcher child")
        action = "sleep 0.2\nexit 7\n" if name == first else "trap 'exit 0' TERM\nwhile true; do sleep 0.1; done\n"
        path.write_text(f"#!/bin/bash\necho $$ > {shlex.quote(str(tmp_path / (name + '.pid')))}\n" + action)
        path.chmod(0o700)
    build = Path(__file__).resolve().parents[2] / "build.sh"
    command = [
        "bash",
        "-c",
        f"source {shlex.quote(str(build))}\nADMIN_SERVER_BINARY={shlex.quote(str(binaries['admin']))}\nMultiRAG_SERVER_BINARY={shlex.quote(str(binaries['api']))}\nrun",
    ]
    journal.event("reserve-process", command=command, cwd=str(tmp_path), group="inherited registered runner group")
    process = subprocess.Popen(command, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    journal.event("spawned", pid=process.pid, pgid=os.getpgid(process.pid))
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 7, (stdout, stderr)
    for name in ("admin", "api"):
        pid = int((tmp_path / (name + ".pid")).read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        journal.event("process-cleanup-readback", pid=pid, absent=True)
