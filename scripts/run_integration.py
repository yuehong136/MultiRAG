"""Prepare only selected services before application imports, then run pytest.

Use ``make integration TESTS=tests/integration/test_async_engine.py`` or
``make integration-db``. Arguments after ``--`` are forwarded to pytest.
"""

import argparse
import json
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.support import services
from tests.support.integration_suites import DATABASE_TESTS, INTEGRATION, SUITES, required_services, suite_paths


def target_arguments(args: list[str]) -> list[str]:
    """Keep pytest option values (for example ``-k api``) out of file selection."""
    value_options = {
        "-k",
        "-m",
        "-c",
        "-o",
        "-n",
        "--numprocesses",
        "--dist",
        "--override-ini",
        "--rootdir",
        "--basetemp",
        "--confcutdir",
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "--junitxml",
        "--junit-xml",
        "--test-report-dir",
        "--cov",
        "--cov-report",
        "--cov-config",
        "--log-file",
    }
    targets = []
    values = iter(args)
    for arg in values:
        if arg in value_options:
            next(values, None)
        elif not arg.startswith("-"):
            targets.append(arg)
    return targets


def selected_paths(suite: str, args: list[str]) -> list[Path]:
    targets = target_arguments(args)
    if any(Path(arg).is_dir() for arg in targets):
        raise ValueError("Use --suite or individual test files; directory targets obscure service dependencies")
    explicit = [Path(arg.split("::", 1)[0]).resolve() for arg in targets if arg.split("::", 1)[0].endswith(".py")]
    if any(not path.is_relative_to(INTEGRATION) or not path.is_file() for path in explicit):
        raise ValueError("Integration targets must be existing files under tests/integration")
    return explicit or suite_paths(suite)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=SUITES, default="core")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    args = options.pytest_args
    if args[:1] == ["--"]:
        args = args[1:]
    try:
        paths = selected_paths(options.suite, args)
    except ValueError as exc:
        parser.error(str(exc))
    if not paths:
        parser.error("No tests selected")
    explicit = any(arg.split("::", 1)[0].endswith(".py") for arg in target_arguments(args))
    if not explicit:
        args = [*(str(path) for path in paths), *args]
    if options.suite in {"all", "consumer"}:
        args.append("--include-external-consumer")
    if options.suite == "consumer":
        args.extend(["-m", "external_consumer"])
    # DB profiles can be isolated per xdist worker; the remaining legacy
    # suites still share process globals and have not earned parallel execution.
    effective_args = [*shlex.split(os.environ.get("PYTEST_ADDOPTS", "")), *args]
    parallel = any(arg.startswith(("-n", "--numprocesses")) for arg in effective_args)
    if parallel and any(path.name not in DATABASE_TESTS for path in paths):
        parser.error("Parallel execution is limited to the isolated database suite")
    if parallel and not any(arg.startswith("--dist") for arg in args):
        args.append("--dist=loadfile")
    report_dir = options.report_dir or ROOT / ".test-results" / (time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}")
    report_dir = report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.extend(["--durations=25", f"--junitxml={report_dir / 'junit.xml'}", f"--test-report-dir={report_dir}"])
    required = sorted(required_services(paths))
    manifest: dict[str, Any] = {
        "suite": options.suite,
        "paths": [str(path.relative_to(ROOT)) for path in paths],
        "unselected_paths": [str(path.relative_to(ROOT)) for path in suite_paths("all") if path not in paths],
        "required_services": required,
        "services": {},
        "exit_code": 1,
    }
    start = time.monotonic()
    saved_env = {key: os.environ.get(key) for key in ("REQUIRE_SERVICES", "MULTIRAG_CONFIG_OVERLAY_FILE")}
    from common.config_utils import CONFIGS

    try:
        with services.ServiceManager(CONFIGS) as manager, tempfile.TemporaryDirectory(prefix="multirag-integration-") as directory:
            services.ACTIVE_MANAGER = manager
            os.environ["REQUIRE_SERVICES"] = "1"
            manifest["services"] = manager.ready
            for service in required:
                manager.ensure(service)
            manifest["prepare_seconds"] = round(time.monotonic() - start, 4)
            # Typed config and child processes read overlays, while legacy code
            # reads CONFIGS. Keep them on exactly the same temporary endpoints.
            overlay = Path(directory) / "services.json"
            overlay.write_text(json.dumps(CONFIGS))
            overlay.chmod(0o600)
            os.environ["MULTIRAG_CONFIG_OVERLAY_FILE"] = str(overlay)
            import pytest

            print(f"Integration suite={options.suite}; services={','.join(required)}; reports={report_dir}", flush=True)
            manifest["exit_code"] = int(pytest.main(args))
    except services.ServiceError as exc:
        # ServiceManager intentionally produces credential-free messages.
        print(str(exc), file=sys.stderr)
        manifest["exit_code"] = 1
        manifest["environment_error"] = str(exc)
    except Exception as exc:
        manifest["exit_code"] = 1
        manifest["environment_error"] = type(exc).__name__
        print(f"Integration environment failed ({type(exc).__name__}); see run.json", file=sys.stderr)
    finally:
        services.ACTIVE_MANAGER = None
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        manifest["total_seconds"] = round(time.monotonic() - start, 4)
        target = report_dir / "run.json"
        target.write_text(json.dumps(manifest, indent=2) + "\n")
        target.chmod(0o600)
    return int(manifest["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
