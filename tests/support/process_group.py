"""Process groups of test-owned Web servers: npm or node and the Vite tree they start."""

import os
import signal
import subprocess
import sys
from typing import Any

# Each server owns a group to stop: a new session on POSIX (start_new_session),
# a console process group on Windows.
if sys.platform == "win32":
    NEW_PROCESS_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    NEW_PROCESS_GROUP = 0


def stop_console_group(child: subprocess.Popen[Any]) -> None:
    """Windows counterpart of killpg: stop the child's group, and no other process."""
    # Ctrl+Break reaches the group only through a console we share with it;
    # without one (OSError) or an exit in time, taskkill /T ends the tree.
    try:
        os.kill(child.pid, signal.CTRL_BREAK_EVENT)
        child.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, check=False)
        child.wait(timeout=10)
