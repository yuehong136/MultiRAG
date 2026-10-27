"""The durable append journal behind validation evidence, on POSIX and Windows alike."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import resource_journal
from scripts.resource_journal import Journal

ROOT = Path(__file__).resolve().parents[2]

WAITER = """
import sys
from pathlib import Path

from scripts.resource_journal import Journal

journal = Journal(Path(sys.argv[1]))
print("opened", flush=True)
try:
    journal.event("after-lock")
finally:
    journal.close()
"""

WRITER = """
import sys
from pathlib import Path

from scripts.resource_journal import Journal

journal = Journal(Path(sys.argv[1]))
try:
    for index in range(int(sys.argv[3])):
        journal.event("append", writer=sys.argv[2], index=index, padding="x" * 4096)
finally:
    journal.close()
"""


def test_create_never_replaces_and_append_never_recreates(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    journal = Journal(path, create=True)
    try:
        journal.event("first")
    finally:
        journal.close()
    with pytest.raises(FileExistsError):
        Journal(path, create=True)
    with pytest.raises(FileNotFoundError):
        Journal(tmp_path / "missing.jsonl")
    assert [json.loads(line)["event"] for line in path.read_bytes().splitlines()] == ["first"]


def test_events_are_lf_terminated_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    journal = Journal(path, create=True)
    try:
        journal.event("first", value=1)
        journal.event("second", value=2)
    finally:
        journal.close()
    raw = path.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    assert [json.loads(line)["value"] for line in raw.split(b"\n")[:-1]] == [1, 2]


def test_concurrent_writers_keep_every_event_whole(tmp_path: Path) -> None:
    path = tmp_path / "shared.jsonl"
    Journal(path, create=True).close()
    writers = [subprocess.Popen([sys.executable, "-c", WRITER, str(path), str(writer), "40"], cwd=ROOT) for writer in range(4)]
    assert [writer.wait(timeout=300) for writer in writers] == [0, 0, 0, 0]
    events = [json.loads(line) for line in path.read_bytes().splitlines()]
    assert sorted((event["writer"], event["index"]) for event in events) == sorted((str(writer), index) for writer in range(4) for index in range(40))


def test_writer_in_another_process_waits_for_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "shared.jsonl"
    holder = Journal(path, create=True)
    try:
        resource_journal._lock(holder.fd)
        try:
            waiter = subprocess.Popen([sys.executable, "-c", WAITER, str(path)], cwd=ROOT, stdout=subprocess.PIPE, text=True)
            assert waiter.stdout is not None and waiter.stdout.readline().strip() == "opened"
            with pytest.raises(subprocess.TimeoutExpired):
                waiter.wait(timeout=2)
            assert path.read_bytes() == b""
        finally:
            resource_journal._unlock(holder.fd)
    finally:
        holder.close()
    assert waiter.wait(timeout=60) == 0
    assert [json.loads(line)["event"] for line in path.read_bytes().splitlines()] == ["after-lock"]


def test_link_is_refused_where_open_follows_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Without O_NOFOLLOW (Windows) os.open follows a final link, so the opened path is checked.
    path = tmp_path / "run.jsonl"
    Journal(path, create=True).close()
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    link = os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    with patch("os.lstat", return_value=link), patch("os.close", wraps=os.close) as close, pytest.raises(ValueError, match="regular file"):
        Journal(path)
    close.assert_called_once()
    assert path.read_bytes() == b""
