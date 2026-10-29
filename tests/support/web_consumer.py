"""Independent Web checkout driven by the external consumer acceptance tests."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from tests.support.resource_journal import ResourceJournal

ROOT = Path(__file__).resolve().parents[2]


def tsx_runner(checkout: Path) -> Path:
    # npm installs a POSIX shell shim and, on Windows, a .cmd shim beside it;
    # CreateProcess cannot execute the shell script (WinError 193).
    return checkout / "node_modules" / ".bin" / ("tsx.cmd" if sys.platform == "win32" else "tsx")


def private_consumer_file(path: Path, value: str, journal: ResourceJournal, owned: list[Path]) -> None:
    journal.event("reserve-private-consumer-file", path=str(path), content_sha256=hashlib.sha256(value.encode()).hexdigest())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    owned.append(path)
    try:
        with os.fdopen(descriptor, "w", closefd=False) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def pinned_web_checkout() -> tuple[Path, str]:
    checkout = Path(os.environ["WEB_COMPLETION_CHECKOUT"])
    web_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    pin = json.loads((ROOT / ".github/consumer-web.json").read_text())["ref"]
    assert web_sha == pin, "Actual Web checkout must equal the consumer pin"
    consumer_status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", "src", "scripts", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.json", "tsconfig.app.json"],
        cwd=checkout,
        text=True,
    )
    assert not consumer_status, "Pinned Web consumer source/configuration must be clean: " + consumer_status
    return checkout, web_sha
