"""Prepare only selected services before application imports, then run pytest.

Use ``make integration TESTS=tests/integration/test_async_engine.py`` or
``make integration-db``. Arguments after ``--`` are forwarded to pytest.
"""

import argparse
import importlib.metadata
import json
import os
import platform
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.support import services
from tests.support.integration_suites import EVALS, INTEGRATION, PARALLEL_TESTS, SUITES, required_services, suite_paths


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
        "--integration-seed",
        "--fixture-jobs",
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
    boundary = EVALS if suite.startswith("eval") else INTEGRATION
    if any(not path.is_relative_to(boundary) or not path.is_file() for path in explicit):
        raise ValueError(f"Test targets must be existing files under {boundary.relative_to(ROOT)}")
    return explicit or suite_paths(suite)


def service_hosts(configs: dict[str, Any]) -> list[str]:
    """Allow owned listeners and configured infrastructure, never model endpoints."""
    hosts = {"127.0.0.1", "::1", "localhost"}
    for service, key in [("postgresql", "host"), ("redis", "host"), ("minio", "host"), ("milvus", "hosts"), ("infinity", "uri")]:
        value = configs.get(service, {}).get(key)
        if isinstance(value, str) and value:
            host = urlsplit(value if "://" in value else "//" + value).hostname
            if host:
                hosts.add(host)
    return sorted(hosts)


def parallel_arguments(paths: list[Path], args: list[str], default_workers: int) -> tuple[list[str], str]:
    """Use two workers only for audited multi-file selections; honor explicit -n."""
    result = list(args)
    effective = [*shlex.split(os.environ.get("PYTEST_ADDOPTS", "")), *args]
    workers: str | None = None
    values = iter(effective)
    for arg in values:
        if arg in {"-n", "--numprocesses"}:
            workers = next(values, None)
            if workers is None:
                raise ValueError(f"{arg} requires a worker count")
        elif arg.startswith("--numprocesses="):
            workers = arg.split("=", 1)[1]
        elif arg.startswith("-n"):
            workers = arg[2:].removeprefix("=")
    audited = all(path.name in PARALLEL_TESTS for path in paths)
    if workers is None:
        automatic = default_workers if audited and len(paths) > 1 and not {"--collect-only", "--co"}.intersection(effective) else 0
        workers = str(automatic)
        if automatic:
            result.extend(["-n", workers])
    if workers != "0":
        if not audited:
            raise ValueError("Parallel execution requires an audited isolated core file; Infinity and quality evals run separately")
        if not any(arg.startswith("--dist") for arg in effective):
            result.append("--dist=worksteal")
    return result, workers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=SUITES, default="core")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--workers", type=int, choices=[0, 1, 2], default=2, help="Default workers for audited multi-file runs; explicit pytest -n takes precedence")
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
    # Only the explicitly audited file set may share infrastructure across workers.
    # Each process retains its own scratch database, globals and owned resources.
    try:
        args, workers = parallel_arguments(paths, args, options.workers)
    except ValueError as exc:
        parser.error(str(exc))
    report_dir = options.report_dir or ROOT / ".test-results" / (time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}")
    report_dir = report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.extend(["--durations=25", f"--junitxml={report_dir / 'junit.xml'}", f"--test-report-dir={report_dir}"])
    required = sorted(required_services(paths))
    manifest: dict[str, Any] = {
        "suite": options.suite,
        "workers": workers,
        "serial_only_paths": [str(path.relative_to(ROOT)) for path in paths if path.name not in PARALLEL_TESTS],
        "paths": [str(path.relative_to(ROOT)) for path in paths],
        "unselected_paths": [str(path.relative_to(ROOT)) for path in suite_paths("all") if path not in paths],
        "required_services": required,
        "services": {},
        "exit_code": 1,
        "environment": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
            "packages": {name: importlib.metadata.version(name) for name in ("pytest", "pytest-xdist", "pymilvus")},
        },
    }
    start = time.monotonic()
    saved_env = {key: os.environ.get(key) for key in ("REQUIRE_SERVICES", "MULTIRAG_CONFIG_OVERLAY_FILE", "LITELLM_LOCAL_MODEL_COST_MAP", "HF_HUB_DISABLE_TELEMETRY")}
    from common.config_utils import CONFIGS

    try:
        with services.ServiceManager(CONFIGS) as manager, tempfile.TemporaryDirectory(prefix="multirag-integration-") as directory:
            services.ACTIVE_MANAGER = manager
            os.environ["REQUIRE_SERVICES"] = "1"
            os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
            os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
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
            hosts = service_hosts(CONFIGS)
            if options.suite == "eval-generation" and os.environ.get("MULTIRAG_EVAL_BASE_URL"):
                host = urlsplit(os.environ["MULTIRAG_EVAL_BASE_URL"]).hostname
                if host:
                    hosts.append(host)
            args.extend(["--allow-unix-socket", "--allow-hosts=" + ",".join(hosts)])
            manifest["network_policy"] = "loopback-and-configured-services"
            import pytest

            print(f"Integration suite={options.suite}; workers={workers}; services={','.join(required)}; reports={report_dir}", flush=True)
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
