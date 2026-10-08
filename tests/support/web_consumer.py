"""Independent Web checkout driven by the external consumer acceptance tests."""

import sys
from pathlib import Path


def tsx_runner(checkout: Path) -> Path:
    # npm installs a POSIX shell shim and, on Windows, a .cmd shim beside it;
    # CreateProcess cannot execute the shell script (WinError 193).
    return checkout / "node_modules" / ".bin" / ("tsx.cmd" if sys.platform == "win32" else "tsx")
