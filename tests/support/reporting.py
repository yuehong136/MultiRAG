"""Machine-readable execution evidence and strict integration skip semantics."""

import hashlib
import json
import os
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from tests.support.integration_suites import EVALS, INTEGRATION

_STATE = pytest.StashKey[dict[str, Any]]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("multirag")
    group.addoption("--test-report-dir", default=None, help="Write execution-*.json and live events with timings and selected test outcomes")
    group.addoption("--include-external-consumer", action="store_true", help="Run tests requiring the independently installed Web checkout")
    group.addoption("--integration-seed", type=int, default=None, help="Reproducibly reorder integration files and cases to expose fixture dependencies")
    group.addoption("--fixture-jobs", type=int, choices=[1, 2], default=2, help="Independent collection creation concurrency; use 1 for serial performance baselines")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "external_consumer: requires an independently installed consumer checkout")
    config.addinivalue_line("markers", "quality: independent versioned model quality evaluation")
    config.stash[_STATE] = {"started": time.monotonic(), "tests": {}, "collection": [], "excluded": [], "selected": [], "collection_seconds": None}
    directory = config.getoption("--test-report-dir")
    if directory:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        events = root / f"events-{os.environ.get('PYTEST_XDIST_WORKER', 'main')}.jsonl"
        events.touch(mode=0o600)
        events.write_text("")
        config.stash[_STATE]["events_path"] = events


def _record_event(config: pytest.Config, nodeid: str, phase: str, outcome: str, seconds: float = 0) -> None:
    path = config.stash[_STATE].get("events_path")
    if path:
        with path.open("a") as stream:
            stream.write(json.dumps({"nodeid": nodeid, "phase": phase, "outcome": outcome, "seconds": round(seconds, 4)}) + "\n")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    _record_event(item.config, item.nodeid, "setup", "started")


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    report = yield
    if report.skipped and (collector.path.is_relative_to(INTEGRATION) or collector.path.is_relative_to(EVALS)) and os.environ.get("REQUIRE_SERVICES"):
        report.outcome = "failed"
        report.longrepr = f"Selected integration module skipped under REQUIRE_SERVICES=1: {report.longrepr}"
    collector.config.stash[_STATE]["collection"].append({"nodeid": collector.nodeid, "outcome": report.outcome})
    return report


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    excluded = []
    for item in list(items):
        if item.path.is_relative_to(INTEGRATION):
            item.add_marker(pytest.mark.integration)
        elif item.path.is_relative_to(EVALS):
            item.add_marker(pytest.mark.quality)
        if item.get_closest_marker("external_consumer") and not config.getoption("--include-external-consumer"):
            items.remove(item)
            excluded.append(item)
    if excluded:
        config.hook.pytest_deselected(items=excluded)
    seed = config.getoption("--integration-seed")
    if seed is not None:

        def digest(value: str) -> str:
            return hashlib.sha256(f"{seed}:{value}".encode()).hexdigest()

        items.sort(key=lambda item: (digest(item.nodeid.split("::")[0]), digest(item.nodeid)))
    config.stash[_STATE]["seed"] = seed
    config.stash[_STATE]["fixture_jobs"] = config.getoption("--fixture-jobs")


def pytest_deselected(items: list[pytest.Item]) -> None:
    if items:
        items[0].config.stash[_STATE]["excluded"].extend(item.nodeid for item in items)


def pytest_collection_finish(session: pytest.Session) -> None:
    state = session.config.stash[_STATE]
    state["collection_seconds"] = round(time.monotonic() - state["started"], 4)
    state["selected"] = [item.nodeid for item in session.items]


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if report.skipped and (item.get_closest_marker("integration") or item.get_closest_marker("quality")) and os.environ.get("REQUIRE_SERVICES") and not hasattr(report, "wasxfail"):
        report.outcome = "failed"
        report.longrepr = f"Selected integration test skipped under REQUIRE_SERVICES=1: {report.longrepr}"
    state = item.config.stash[_STATE]
    phases = state["tests"].setdefault(item.nodeid, {})
    phases[report.when] = {"outcome": report.outcome, "seconds": round(report.duration, 4)}
    _record_event(item.config, item.nodeid, report.when, report.outcome, report.duration)
    return report


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    directory = session.config.getoption("--test-report-dir")
    if not directory:
        return
    state = session.config.stash[_STATE].copy()
    state.pop("events_path", None)
    state["total_seconds"] = round(time.monotonic() - state.pop("started"), 4)
    state["exit_code"] = int(exitstatus)
    state["worker"] = os.environ.get("PYTEST_XDIST_WORKER", "main")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"execution-{state['worker']}.json"
    path.write_text(json.dumps(state, indent=2) + "\n")
    path.chmod(0o600)
