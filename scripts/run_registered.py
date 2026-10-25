"""Run Python/Go/Web commands behind a durable resource gate (POSIX only).

No application or third-party imports occur here. External services and resources
still require case helpers; this runner owns only its process group and paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import FrameType
from typing import Any

from resource_journal import Journal


class Cancelled(Exception):
    """A caught termination signal; retain the signal and raw child result."""

    def __init__(self, signum: int) -> None:
        self.signum = signum


def stop_group(process: subprocess.Popen[bytes]) -> bool:
    """Stop only the session created for this command, including descendants."""
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        # The leader can exit while a descendant still holds the process group.
        time.sleep(0.05)
    process.wait()
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return True
    return False


def child(journal_path: str, command: list[str]) -> int:
    """The actual PID must fsync its identity before replacing itself via exec."""
    journal = Journal(Path(journal_path))
    try:
        journal.event("child-before-exec", run_id=os.environ["MULTIRAG_RUN_ID"], pid=os.getpid(), parent_pid=os.getppid(), pgid=os.getpgrp())
    finally:
        journal.close()
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    os.execvpe(command[0], command, os.environ)
    return 127


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True, help="Existing private evidence directory")
    parser.add_argument("--label", required=True)
    parser.add_argument("--allow-model-download", action="store_true", help="Allow model downloads only into registered cache paths")
    parser.add_argument("--input", action="append", type=Path, required=True, help="Source/config/lock/consumer inputs to hash")
    parser.add_argument("--borrowed", action="append", default=[], help="Non-secret borrowed resource identity; never deleted")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    command = options.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("command required")
    root = options.evidence_root.resolve(strict=True)
    run_id = uuid.uuid4().hex
    owned = root / run_id
    journal_path = root / (run_id + ".jsonl")
    journal = Journal(journal_path, create=True)
    process: subprocess.Popen[bytes] | None = None
    created = False
    raw_exit: int | None = None
    exit_code = 125
    cancelled: int | None = None
    previous: dict[signal.Signals, Any] = {}

    def cancel(signum: int, _frame: FrameType | None) -> None:
        raise Cancelled(signum)

    try:
        journal.event(
            "reserve-run",
            run_id=run_id,
            label=options.label,
            pid=os.getpid(),
            parent_pid=os.getppid(),
            command=command,
            cwd=str(Path.cwd()),
            owned=str(owned),
            borrowed=options.borrowed,
            runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            inputs={str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in options.input},
            coverage="process-group/tmp/cache only; external resources require reserve-before-create case helpers",
        )
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, cancel)
        owned.mkdir(mode=0o700)
        created = True
        env = os.environ.copy()
        folders = {
            "TMPDIR": "tmp",
            "TMP": "tmp",
            "TEMP": "tmp",
            "HF_HOME": "models/huggingface",
            "TORCH_HOME": "models/torch",
            "XDG_CACHE_HOME": "cache",
            "GOCACHE": "go-cache",
            "GOMODCACHE": "go-modules",
            "TIKTOKEN_CACHE_DIR": "models/tiktoken",
            "MODELSCOPE_CACHE": "models/modelscope",
            "SENTENCE_TRANSFORMERS_HOME": "models/sentence-transformers",
            "NUMBA_CACHE_DIR": "cache/numba",
            "MPLCONFIGDIR": "cache/matplotlib",
            "GOTMPDIR": "go-tmp",
            "UV_CACHE_DIR": "uv-cache",
            "npm_config_cache": "npm-cache",
            "REST_DOCUMENT_RESOURCE_DIR": "cases",
        }
        for key, relative in folders.items():
            directory = owned / relative
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            env[key] = str(directory)
        env.update({"MULTIRAG_RUN_ID": run_id, "MULTIRAG_RUN_JOURNAL": str(journal_path), "MULTIRAG_PROVIDER_TEST_JOURNAL": str(journal_path), "PYTHONDONTWRITEBYTECODE": "1"})
        # Override inherited model cache aliases that would otherwise bypass HF_HOME.
        env["HUGGINGFACE_HUB_CACHE"] = str(owned / "models/huggingface/hub")
        env["HF_HUB_CACHE"] = env["HUGGINGFACE_HUB_CACHE"]
        env["TRANSFORMERS_CACHE"] = str(owned / "models/transformers")
        if not options.allow_model_download:
            env["HF_HUB_OFFLINE"] = "1"
            env["TRANSFORMERS_OFFLINE"] = "1"
        journal.event("reserve-process", run_id=run_id, parent_pid=os.getpid(), session="new session, pgid=child pid", output=str(root / (run_id + ".output")))
        with (root / (run_id + ".output")).open("xb") as output:
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
            try:
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--child", str(journal_path), *command], env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
                )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
            journal.event("spawned", run_id=run_id, pid=process.pid, pgid=process.pid)
            raw_exit = process.wait()
            exit_code = raw_exit if raw_exit >= 0 else 128 - raw_exit
    except Cancelled as exc:
        cancelled = exc.signum
        exit_code = 128 + exc.signum
    finally:
        # Repeated cancellation must not interrupt owned cleanup.
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        try:
            absent = stop_group(process) if process is not None else True
            if process is not None:
                raw_exit = process.returncode
            # Keep case journals outside the temporary subtree for independent readback.
            if created:
                cases = owned / "cases"
                if cases.exists():
                    journal.event("retain-case-journals", source=str(cases), destination=str(root / (run_id + ".cases")))
                    cases.rename(root / (run_id + ".cases"))
                shutil.rmtree(owned)
            if not absent or owned.exists():
                exit_code = 125
            journal.event(
                "finished",
                run_id=run_id,
                raw_exit=raw_exit,
                exit_code=exit_code,
                cancelled_signal=cancelled,
                pgid=process.pid if process else None,
                process_group_absent=absent,
                owned_absent=not owned.exists(),
            )
        finally:
            journal.close()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    print(json.dumps({"run_id": run_id, "journal": str(journal_path), "exit_code": exit_code}))
    return exit_code


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        sys.exit(child(sys.argv[2], sys.argv[3:]))
    sys.exit(main())
