"""Small stdlib-only, durable append journal shared by validation entry points."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from pathlib import Path
from typing import Any


class Journal:
    """Never recreate a missing journal or replace an existing event history."""

    def __init__(self, path: Path, *, create: bool = False) -> None:
        self.path = path
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        self.fd = os.open(path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(self.fd).st_mode):
            self.close()
            raise ValueError("journal must be a regular file")
        try:
            os.fsync(self.fd)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            self.close()
            raise

    def event(self, event: str, **fields: Any) -> None:
        payload = (json.dumps({"event": event, "time_ns": time.time_ns(), **fields}, default=str) + "\n").encode()
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        try:
            view = memoryview(payload)
            while view:
                count = os.write(self.fd, view)
                if count <= 0:
                    raise OSError("short journal write")
                view = view[count:]
            os.fsync(self.fd)
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)

    def close(self) -> None:
        os.close(self.fd)
