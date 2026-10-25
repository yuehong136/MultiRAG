"""Subprocess regressions for the pre-exec resource gate; stdlib only."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts/run_registered.py"
sys.path.insert(0, str(ROOT))

from tests.support.resource_journal import ResourceJournal


class RegisteredRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.case_journal = ResourceJournal(self.id())
        self.root = Path(tempfile.gettempdir()) / ("gate-case-" + self.case_journal.run_id)
        self.case_journal.event("reserve-path", path=str(self.root))
        self.root.mkdir(mode=0o700)
        self.addCleanup(self.cleanup_case)

    def cleanup_case(self) -> None:
        retained = self.case_journal.path.parent / (self.case_journal.run_id + ".evidence")
        self.case_journal.event("reserve-retained-evidence", path=str(retained))
        shutil.copytree(self.root, retained)
        shutil.rmtree(self.root)
        self.case_journal.event("path-cleanup-readback", path=str(self.root), absent=not self.root.exists())

    def command(self, program: str) -> list[str]:
        return [sys.executable, str(RUNNER), "--evidence-root", str(self.root), "--label", self.id(), "--input", str(RUNNER), "--", sys.executable, "-c", program]

    def events(self) -> list[dict[str, object]]:
        return [json.loads(line) for path in self.root.glob("*.jsonl") for line in path.read_text().splitlines()]

    def assert_clean(self, events: list[dict[str, object]]) -> None:
        reserve = next(event for event in events if event["event"] == "reserve-run")
        final = next(event for event in events if event["event"] == "finished")
        self.assertFalse(Path(str(reserve["owned"])).exists())
        self.assertTrue(final["process_group_absent"])
        self.assertTrue(final["owned_absent"])
        pid = int(str(final["pgid"]))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)

    def test_success_and_failure_have_distinct_append_only_histories(self) -> None:
        for code in (0, 7):
            result = subprocess.run(self.command(f'import os,pathlib; pathlib.Path(os.environ["HF_HOME"]).joinpath("partial").write_text("x"); raise SystemExit({code})'), capture_output=True)
            self.assertEqual(result.returncode, code, result.stderr)
        paths = list(self.root.glob("*.jsonl"))
        self.assertEqual(len(paths), 2)
        for path in paths:
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertLess(next(i for i, event in enumerate(events) if event["event"] == "reserve-run"), next(i for i, event in enumerate(events) if event["event"] == "child-before-exec"))
            self.assert_clean(events)
        self.assertEqual({event["raw_exit"] for event in self.events() if event["event"] == "finished"}, {0, 7})

    def test_cancel_kills_descendant_and_removes_partial_model(self) -> None:
        ready = self.root / "ready"
        program = f'import os,pathlib,subprocess,sys,time; subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); pathlib.Path(os.environ["HF_HOME"]).joinpath("partial").write_text("x"); pathlib.Path({str(ready)!r}).touch(); time.sleep(60)'
        process = subprocess.Popen(self.command(program), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists())
        process.send_signal(signal.SIGTERM)
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, (output, error))
        events = self.events()
        final = next(event for event in events if event["event"] == "finished")
        self.assertEqual(final["cancelled_signal"], 15)
        self.assertEqual(final["raw_exit"], -15)
        self.assert_clean(events)

    def test_missing_evidence_directory_never_imports_business(self) -> None:
        marker = self.root / "business-imported"
        command = self.command(f"from pathlib import Path; Path({str(marker)!r}).touch()")
        command[3] = str(self.root / "missing")
        result = subprocess.run(command, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_child_cannot_exec_without_durable_journal(self) -> None:
        marker = self.root / "import-marker"
        (self.root / "business.py").write_text(f"from pathlib import Path; Path({str(marker)!r}).touch()")
        result = subprocess.run(
            [sys.executable, str(RUNNER), "--child", str(self.root / "missing.jsonl"), sys.executable, "-c", "import business"],
            cwd=self.root,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def load_runner(self) -> Any:
        sys.path.insert(0, str(ROOT / "scripts"))
        self.addCleanup(lambda: sys.path.remove(str(ROOT / "scripts")))
        spec = importlib.util.spec_from_file_location("registered_runner_under_test", RUNNER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_cancel_between_spawn_and_assignment_keeps_pid_for_cleanup(self) -> None:
        module = self.load_runner()
        real_launch = module.subprocess.Popen

        def launch(*args: Any, **kwargs: Any) -> Any:
            process = real_launch(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGTERM)
            return process

        with patch.object(module.subprocess, "Popen", side_effect=launch):
            result = module.main(self.command("import time; time.sleep(60)")[2:])
        self.assertEqual(result, 143)
        self.assert_clean(self.events())

    def test_cleanup_failure_records_final_nonzero_exit_and_raw_success(self) -> None:
        module = self.load_runner()
        real_stop = module.stop_group

        def stop(process: Any) -> bool:
            real_stop(process)
            return False

        with patch.object(module, "stop_group", side_effect=stop):
            result = module.main(self.command("pass")[2:])
        self.assertEqual(result, 125)
        finished = next(event for event in self.events() if event["event"] == "finished")
        self.assertEqual(finished["raw_exit"], 0)
        self.assertEqual(finished["exit_code"], 125)

    def test_journal_create_write_and_fsync_failure_never_launch(self) -> None:
        module = self.load_runner()
        arguments = ["--evidence-root", str(self.root), "--label", "failure", "--input", str(RUNNER), "--", sys.executable, "-c", "raise AssertionError('business executed')"]
        for operation in ("os.open", "os.write", "os.fsync"):
            with self.subTest(operation=operation), patch(operation, side_effect=OSError("unavailable")), patch.object(module.subprocess, "Popen") as launch:
                with self.assertRaises(OSError):
                    module.main(arguments)
                launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
