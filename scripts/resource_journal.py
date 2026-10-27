"""Small stdlib-only, durable append journal shared by validation entry points."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    import errno
    import msvcrt

    # Windows byte-range locks are mandatory, so writers serialize on one byte
    # far past any journal's end, where neither appends nor readers reach.
    _LOCK_OFFSET = 1 << 62

    def _lock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno != errno.EACCES:
                    raise
                time.sleep(0.01)
            else:
                return

    def _unlock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def _fsync_directory(_path: Path) -> None:
        """Windows cannot open a directory as a descriptor. NTFS records the new
        entry in its metadata log, which os.fsync (FlushFileBuffers) of the
        journal itself flushes."""

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

    def _fsync_directory(path: Path) -> None:
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _opened_through_link(path: Path) -> bool:
    """Without O_NOFOLLOW (Windows), os.open followed a final link; refuse it once open."""
    if hasattr(os, "O_NOFOLLOW"):
        return False
    status = os.lstat(path)
    return stat.S_ISLNK(status.st_mode) or bool(getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


class Journal:
    """Never recreate a missing journal or replace an existing event history."""

    def __init__(self, path: Path, *, create: bool = False) -> None:
        self.path = path
        # O_BINARY keeps Windows from writing each LF as CRLF.
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        self.fd = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(self.fd).st_mode) or _opened_through_link(path):
                raise ValueError("journal must be a regular file")
            os.fsync(self.fd)
            _fsync_directory(path.parent)
        except BaseException:
            self.close()
            raise

    def event(self, event: str, **fields: Any) -> None:
        payload = (json.dumps({"event": event, "time_ns": time.time_ns(), **fields}, default=str) + "\n").encode()
        _lock(self.fd)
        try:
            view = memoryview(payload)
            while view:
                count = os.write(self.fd, view)
                if count <= 0:
                    raise OSError("short journal write")
                view = view[count:]
            os.fsync(self.fd)
        finally:
            _unlock(self.fd)

    def close(self) -> None:
        os.close(self.fd)
