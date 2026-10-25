"""Regression tests for selection, owned services and fail-closed reporting."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from scripts.run_integration import main, parallel_arguments, registered_overlay, selected_paths
from tests.support import services
from tests.support.integration_suites import DATABASE_TESTS, INTEGRATION, ROOT, required_services, suite_paths


def test_database_suite_never_requires_unrelated_services() -> None:
    paths = suite_paths("db")
    assert {path.name for path in paths} == DATABASE_TESTS
    assert required_services(paths) == {"postgresql"}
    assert required_services(suite_paths("infinity")) == {"infinity", "postgresql"}
    assert required_services([INTEGRATION / "new_test.py"]) == {"postgresql", "redis", "minio", "milvus"}


def test_shared_auth_secret_prepares_only_redis() -> None:
    assert required_services([INTEGRATION / "test_shared_auth_secret.py"]) == {"redis"}


def test_metadata_es_dependency_is_prepared_before_collection() -> None:
    assert required_services([INTEGRATION / "test_metadata_lazy_pushdown.py"]) == {"postgresql", "elasticsearch"}
    assert "elasticsearch" in required_services(suite_paths("core"))


def test_keyword_id_contract_prepares_only_disposable_es() -> None:
    assert required_services([INTEGRATION / "test_es_conn_keyword_ids.py"]) == {"elasticsearch"}


def test_infinity_metadata_prepares_sql_and_engine_before_collection() -> None:
    path = INTEGRATION / "test_metadata_infinity_pushdown.py"
    assert path in suite_paths("infinity") and path not in suite_paths("core")
    assert path not in suite_paths("db")
    assert required_services([path]) == {"postgresql", "infinity"}


def test_runtime_storage_protocol_needs_no_infrastructure() -> None:
    assert required_services([INTEGRATION / "test_runtime_cleanup_storage_protocol.py"]) == set()


def test_server_scoring_requires_sql_and_both_search_engines() -> None:
    assert required_services([INTEGRATION / "test_es_server_scoring.py"]) == {"postgresql", "elasticsearch", "opensearch"}


def test_explicit_metadata_es_failure_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METADATA_TEST_ES_URL", "http://owned.invalid:9200")
    monkeypatch.delenv("INTEGRATION_NO_TESTCONTAINERS", raising=False)

    def unavailable(name: str, configs: dict[str, Any]) -> None:
        raise ConnectionError("private test URL")

    monkeypatch.setattr(services, "probe", unavailable)
    monkeypatch.setattr(services.ServiceManager, "_start", lambda *args: pytest.fail("Explicit listener must not be replaced"))
    with services.ServiceManager({}) as manager, pytest.raises(services.ServiceError, match="elasticsearch"):
        manager.ensure("elasticsearch")


@pytest.mark.parametrize("fail_start", [False, True])
def test_metadata_es_container_and_endpoint_are_scoped(monkeypatch: pytest.MonkeyPatch, fail_start: bool) -> None:
    import testcontainers.core.container

    events: list[str] = []
    monkeypatch.delenv("METADATA_TEST_ES_URL", raising=False)
    monkeypatch.delenv("INTEGRATION_NO_TESTCONTAINERS", raising=False)

    class Container:
        def __init__(self, image: str) -> None:
            assert "elasticsearch" in image and "${" not in image

        def with_exposed_ports(self, port: int) -> "Container":
            assert port == 9200
            return self

        def with_env(self, key: str, value: str) -> "Container":
            return self

        def start(self) -> None:
            events.append("start")
            if fail_start:
                raise RuntimeError("startup failed")

        def stop(self) -> None:
            events.append("stop")

        def get_container_host_ip(self) -> str:
            return "127.0.0.1"

        def get_exposed_port(self, port: int) -> int:
            return 54321

    def probe(name: str, configs: dict[str, Any]) -> str:
        if not os.environ.get("METADATA_TEST_ES_URL"):
            raise ConnectionError
        return "8.11.3"

    monkeypatch.setattr(testcontainers.core.container, "DockerContainer", Container)
    monkeypatch.setattr(services, "probe", probe)
    if fail_start:
        with pytest.raises(services.ServiceError, match="Could not provision"):
            with services.ServiceManager({}) as manager:
                manager.ensure("elasticsearch")
    else:
        with services.ServiceManager({}) as manager:
            manager.ensure("elasticsearch")
            assert os.environ["METADATA_TEST_ES_URL"] == "http://127.0.0.1:54321"
            assert manager.ready["elasticsearch"]["source"] == "testcontainers"
    assert events == ["start", "stop"]
    assert "METADATA_TEST_ES_URL" not in os.environ


def test_explicit_node_selection_prepares_only_its_dependencies() -> None:
    paths = selected_paths("core", [str(INTEGRATION / "test_async_engine.py") + "::test_async_engine_select_one", "-q"])
    assert required_services(paths) == {"postgresql"}
    with pytest.raises(ValueError, match="under tests/integration"):
        selected_paths("core", [str(ROOT / "tests/unit/test_app_config.py")])
    with pytest.raises(ValueError, match="directory targets"):
        selected_paths("core", [str(INTEGRATION)])


def test_consumer_suite_contains_every_external_consumer_module() -> None:
    """A newly added Web contract cannot be silently omitted from its suite."""
    import ast

    consumers = set()
    for path in INTEGRATION.glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and node.value.attr == "mark" and node.attr == "external_consumer" for node in ast.walk(tree)):
            consumers.add(path)
    assert set(suite_paths("consumer")) == consumers


def test_pytest_filter_and_ignore_values_are_not_mistaken_for_targets() -> None:
    assert selected_paths("db", ["-k", "api", "--ignore", str(INTEGRATION / "test_async_engine.py")]) == suite_paths("db")


@pytest.mark.parametrize("option", ["-nauto", "-n2", "--numprocesses=2"])
def test_parallel_options_cannot_bypass_storage_isolation_guard(option: str) -> None:
    with pytest.raises(SystemExit) as result:
        main(["--", str(INTEGRATION / "test_infinity_available_filter.py"), option])
    assert result.value.code == 2


def test_inherited_parallel_option_cannot_bypass_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-nauto")
    with pytest.raises(SystemExit) as result:
        main(["--", str(INTEGRATION / "test_infinity_available_filter.py")])
    assert result.value.code == 2


def test_default_parallelism_requires_audited_multiple_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    audited = [INTEGRATION / "test_async_engine.py", INTEGRATION / "test_document_status.py"]
    args, workers = parallel_arguments(audited, ["-q"], 2)
    assert workers == "2" and args[-3:] == ["-n", "2", "--dist=worksteal"]
    for paths in [suite_paths("infinity"), suite_paths("eval"), [INTEGRATION / "future_test.py"], suite_paths("core") + [INTEGRATION / "future_test.py"]]:
        args, workers = parallel_arguments(paths, ["-q"], 2)
        assert workers == "0" and args == ["-q"]


@pytest.mark.parametrize("option", [["-n", "0"], ["-n0"], ["--numprocesses=0"]])
def test_explicit_serial_mode_is_allowed_for_every_suite(monkeypatch: pytest.MonkeyPatch, option: list[str]) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n 2")
    args, workers = parallel_arguments(suite_paths("infinity"), option, 2)
    assert workers == "0" and args == option


def test_inherited_scheduler_and_collection_only_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n 2 --dist=loadfile")
    args, workers = parallel_arguments(suite_paths("db"), ["-q"], 2)
    assert workers == "2" and args == ["-q"]
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    assert parallel_arguments(suite_paths("core"), ["--collect-only"], 2) == (["--collect-only"], "0")


def test_read_only_service_check_has_working_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.check_services import main as check_services

    calls: list[str] = []
    monkeypatch.setattr(services, "probe", lambda name, configs: calls.append(name))
    assert check_services([]) == 0
    assert calls == ["postgresql", "redis", "minio"]


def test_runner_reports_unselected_backends_and_restores_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUIRE_SERVICES", "previous")
    overlay = os.environ.get("MULTIRAG_CONFIG_OVERLAY_FILE")
    monkeypatch.setattr(services, "probe", lambda name, configs: None)
    monkeypatch.setattr(pytest, "main", lambda args: 0)
    assert main(["--suite", "db", "--report-dir", str(tmp_path), "--", "-q"]) == 0
    report = json.loads((tmp_path / "run.json").read_text())
    assert report["required_services"] == ["postgresql"]
    assert "tests/integration/test_infinity_available_filter.py" in report["unselected_paths"]
    assert os.environ["REQUIRE_SERVICES"] == "previous"
    assert os.environ.get("MULTIRAG_CONFIG_OVERLAY_FILE") == overlay


def test_overlay_is_registered_before_creation_and_private_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_mkdir = Path.mkdir
    observed: list[Path] = []

    def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.name.startswith("multirag-integration-"):
            records = [json.loads(p.read_text()) for p in tmp_path.glob("overlay-*.json")]
            assert len(records) == 1 and records[0]["directory"] == str(path)
            assert records[0]["status"] == "registered-before-creation"
            observed.append(path)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    manifest: dict[str, Any] = {}
    with pytest.raises(RuntimeError, match="controlled interruption"), registered_overlay(tmp_path, manifest) as overlay:
        if sys.platform != "win32":  # POSIX permission bits; Windows st_mode carries only the read-only flag
            assert overlay.stat().st_mode & 0o777 == 0o600
            assert overlay.parent.stat().st_mode & 0o777 == 0o700
        overlay.write_text('{"password":"private-test-secret"}')
        raise RuntimeError("controlled interruption")
    journal = Path(manifest["temporary_resource_journal"])
    record = json.loads(journal.read_text())
    assert record["pid"] == os.getpid() and record["started_unix_ns"] > 0
    assert record["cleanup"] == {"overlay_absent": True, "directory_absent": True}
    assert record["status"] == "cleaned" and len(record["overlay_sha256"]) == 64
    assert "private-test-secret" not in journal.read_text()
    assert len(observed) == 1 and not observed[0].exists()


# POSIX signal semantics: Windows has neither SIGKILL nor signal.pause, delivers
# SIGTERM as TerminateProcess (no handler runs), and a venv's python.exe launcher
# has a different PID from the interpreter that registers the overlay.
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery and process identity")
@pytest.mark.parametrize("termination", ["SIGTERM", "SIGKILL"])
def test_interrupted_overlay_is_cleaned_or_exactly_recoverable(tmp_path: Path, termination: str) -> None:
    signum = signal.Signals[termination]
    source = """
import signal, sys
from pathlib import Path
from scripts.run_integration import registered_overlay
with registered_overlay(Path(sys.argv[1]), {}) as overlay:
    overlay.write_text('{"password":"private-child-secret"}')
    print('ready', flush=True)
    signal.pause()
"""
    child = subprocess.Popen([sys.executable, "-u", "-c", source, str(tmp_path)], env={**os.environ, "PYTHONPATH": str(ROOT)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        journal = next(tmp_path.glob("overlay-*.json"))
        record = json.loads(journal.read_text())
        assert record["pid"] == child.pid
        overlay, directory = Path(record["overlay"]), Path(record["directory"])
        child.send_signal(signum)
        child.communicate(timeout=10)
        record = json.loads(journal.read_text())
        if signum == signal.SIGTERM:
            assert child.returncode == 128 + signal.SIGTERM
            assert record["status"] == "cleaned"
            assert record["cleanup"] == {"overlay_absent": True, "directory_absent": True}
        else:
            assert child.returncode == -signal.SIGKILL and record["status"] == "created"
            assert overlay.is_file() and directory.is_dir()
            # Recovery acts only on this child's pre-registered paths.
            overlay.unlink()
            directory.rmdir()
        assert not overlay.exists() and not directory.exists()
        assert "private-child-secret" not in journal.read_text()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def test_existing_service_is_probed_once_and_never_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(services, "probe", lambda name, configs: calls.append(name))
    with services.ServiceManager({}) as manager:
        manager.ensure("postgresql")
        manager.ensure("postgresql")
        assert manager.ready["postgresql"]["source"] == "existing"
    assert calls == ["postgresql"]


def test_service_failure_does_not_expose_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(name: str, configs: dict[str, Any]) -> None:
        raise ConnectionError("password=private-test-secret")

    monkeypatch.setattr(services, "probe", unavailable)
    with services.ServiceManager({}, allow_containers=False) as manager:
        with pytest.raises(RuntimeError) as error:
            manager.ensure("postgresql")
    assert "private-test-secret" not in str(error.value)
    assert "postgresql" in str(error.value)


@pytest.mark.parametrize("fail_start", [False, True])
def test_owned_container_cleanup_also_covers_start_failure(monkeypatch: pytest.MonkeyPatch, fail_start: bool) -> None:
    import testcontainers.redis

    events: list[str] = []
    original = {"host": "original:6379", "password": "original-secret"}
    configs = {"redis": dict(original)}

    class Container:
        def __init__(self, image: str) -> None:
            pass

        def start(self) -> None:
            events.append("start")
            if fail_start:
                raise RuntimeError("private startup details")

        def stop(self) -> None:
            events.append("stop")

        def get_container_host_ip(self) -> str:
            return "localhost"  # what Testcontainers reports for a local daemon

        def get_exposed_port(self, port: int) -> int:
            return 54321

    def probe(name: str, values: dict[str, Any]) -> None:
        if values[name]["host"] == original["host"]:
            raise ConnectionError

    monkeypatch.delenv("INTEGRATION_NO_TESTCONTAINERS", raising=False)
    monkeypatch.setattr(services, "probe", probe)
    monkeypatch.setattr(testcontainers.redis, "RedisContainer", Container)
    if fail_start:
        with pytest.raises(RuntimeError, match="Could not provision"):
            with services.ServiceManager(configs) as manager:
                manager.ensure("redis")
    else:
        with services.ServiceManager(configs) as manager:
            manager.ensure("redis")
            assert configs["redis"]["host"] == "127.0.0.1:54321"
            assert manager.ready["redis"]["source"] == "testcontainers"
    assert events == ["start", "stop"]
    assert configs == {"redis": original}


_FAKE_INFINITY_SDK = """
import os, time
seconds = float(os.environ["FAKE_INFINITY_IMPORT"])
if seconds < 0:
    os._exit(int(-seconds))
time.sleep(seconds)

class _Client:
    def list_databases(self):
        time.sleep(float(os.environ["FAKE_INFINITY_HANDSHAKE"]))
        return type("Result", (), {"error_code": 0})()

    def disconnect(self):
        pass

def connect(address):
    return _Client()
"""


@pytest.mark.parametrize(
    ("import_seconds", "handshake_seconds", "bounds", "error"),
    [
        (1.5, 0, (30, 1), None),  # a slow cold SDK import is not a failed handshake
        (0, 30, (30, 1), subprocess.TimeoutExpired),  # a hung handshake still fails fast
        (30, 0, (1, 1), TimeoutError),  # and so does a hung import
        (-1, 0, (30, 1), ImportError),  # the import raised (Python exit status 1)
        (-3, 0, (30, 1), ChildProcessError),  # the probe process itself died
    ],
)
def test_infinity_probe_bounds_sdk_import_apart_from_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, import_seconds: float, handshake_seconds: float, bounds: tuple[float, float], error: type[Exception] | None
) -> None:
    package = tmp_path / "infinity"
    package.mkdir()
    (package / "__init__.py").write_text(_FAKE_INFINITY_SDK)
    (package / "common.py").write_text("class NetworkAddress:\n    def __init__(self, host, port):\n        pass\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("FAKE_INFINITY_IMPORT", str(import_seconds))
    monkeypatch.setenv("FAKE_INFINITY_HANDSHAKE", str(handshake_seconds))
    monkeypatch.setattr(services, "INFINITY_IMPORT_SECONDS", bounds[0])
    monkeypatch.setattr(services, "INFINITY_HANDSHAKE_SECONDS", bounds[1])
    started = time.monotonic()
    if error is None:
        services.infinity_handshake("127.0.0.1", 23817)
    else:
        with pytest.raises(error):
            services.infinity_handshake("127.0.0.1", 23817)
    assert time.monotonic() - started < 15


def test_scratch_loops_are_ones_async_psycopg_accepts() -> None:
    """uvicorn and asyncio.run() default to Proactor loops on win32, where async psycopg refuses to connect."""
    import asyncio

    import uvicorn

    from tests.support import event_loop

    proactor = getattr(asyncio, "ProactorEventLoop", ())
    factory = uvicorn.Config(object(), loop=event_loop.UVICORN_LOOP, log_config=None).get_loop_factory()
    assert factory is not None
    server_loop = factory()
    server_loop.close()

    async def running_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    assert not isinstance(server_loop, proactor)
    assert not isinstance(event_loop.run(running_loop()), proactor)


@pytest.mark.parametrize("make", ["/usr/bin/make", None])
def test_smoke_make_variables_only_reach_make(monkeypatch: pytest.MonkeyPatch, make: str | None) -> None:
    """Without make the target's command runs this interpreter directly, so variables have no counterpart."""
    from tests.support import smoke

    monkeypatch.setattr(smoke.shutil, "which", lambda name: make if name == "make" else None)
    expected = ["make", "smoke", "UV=uv run --no-sync"] if make else [sys.executable, str(ROOT / "scripts" / "smoke.py")]
    assert smoke.smoke_command("UV=uv run --no-sync") == expected


def _pytest(tmp_path: Path, source: str, *args: str, strict: bool = False) -> subprocess.CompletedProcess[str]:
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    integration: integration\n")
    (tmp_path / "test_sample.py").write_text(source)
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    env.pop("PYTEST_ADDOPTS", None)
    if strict:
        env["REQUIRE_SERVICES"] = "1"
    else:
        env.pop("REQUIRE_SERVICES", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.support.reporting", "-q", f"--test-report-dir={tmp_path / 'reports'}", *args, str(tmp_path / "test_sample.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_selected_skip_fails_and_produces_phase_evidence(tmp_path: Path) -> None:
    result = _pytest(tmp_path, "import pytest\n@pytest.mark.integration\ndef test_missing():\n    pytest.skip('unavailable service')\n", strict=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Selected integration test skipped" in result.stdout
    report = json.loads((tmp_path / "reports/execution-main.json").read_text())
    assert report["exit_code"] == 1
    assert next(iter(report["tests"].values()))["call"]["outcome"] == "failed"
    events = [json.loads(line) for line in (tmp_path / "reports/events-main.jsonl").read_text().splitlines()]
    assert events[0]["outcome"] == "started"
    assert any(event["phase"] == "call" and event["outcome"] == "failed" for event in events)


def test_module_level_skip_cannot_silently_remove_selected_coverage(tmp_path: Path) -> None:
    source = "\n".join(
        [
            "import pytest",
            "from pathlib import Path",
            "from tests.support import reporting",
            "reporting.INTEGRATION = Path(__file__).parent",
            "pytest.skip('missing backend dependency', allow_module_level=True)",
        ]
    )
    result = _pytest(tmp_path, source, strict=True)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Selected integration module skipped" in result.stdout
    report = json.loads((tmp_path / "reports/execution-main.json").read_text())
    assert any(item["nodeid"] == "test_sample.py" and item["outcome"] == "failed" for item in report["collection"])


def test_optional_consumer_is_reported_as_deselected_then_fails_when_selected(tmp_path: Path) -> None:
    source = "import pytest\ndef test_core():\n    pass\n@pytest.mark.integration\n@pytest.mark.external_consumer\ndef test_consumer():\n    pytest.skip('missing checkout')\n"
    result = _pytest(tmp_path, source, strict=True)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "reports/execution-main.json").read_text())
    assert len(report["excluded"]) == 1
    result = _pytest(tmp_path, source, "--include-external-consumer", strict=True)
    assert result.returncode == 1, result.stdout + result.stderr


def test_integration_marker_does_not_capture_unit_tests() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-qq", "-m", "integration", "tests/unit/test_app_config.py", "tests/integration/test_services_connectivity.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/integration/test_services_connectivity.py: 3" in result.stdout
    assert "tests/unit/test_app_config.py:" not in result.stdout


def test_integration_modules_do_not_import_other_test_modules() -> None:
    """A fixture move must not silently reintroduce collection/order dependencies."""
    import ast

    violations = []
    for path in INTEGRATION.glob("test_*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("tests.integration.test_") or (node.level and module.startswith("test_")):
                    violations.append(f"{path.name}:{node.lineno}: {module}")
            elif isinstance(node, ast.Import):
                violations.extend(f"{path.name}:{node.lineno}: {alias.name}" for alias in node.names if alias.name.startswith("tests.integration.test_"))
    assert not violations, "Move shared fixtures/helpers to tests/support:\n" + "\n".join(violations)


def test_network_allowlist_excludes_model_endpoints() -> None:
    from scripts.run_integration import service_hosts

    hosts = service_hosts({"postgresql": {"host": "pg.test"}, "redis": {"host": "cache.test:6379"}, "milvus": {"hosts": "http://vector.test:19530"}, "llm": {"base_url": "https://model.test"}})
    assert "pg.test" in hosts and "cache.test" in hosts and "vector.test" in hosts
    assert "127.0.0.1" in hosts and "model.test" not in hosts


def test_python_socket_guard_blocks_external_provider_before_connection(tmp_path: Path) -> None:
    source = """
import socket
import pytest
from pytest_socket import SocketConnectBlockedError

def test_provider():
    with socket.socket() as client, pytest.raises(SocketConnectBlockedError):
        client.connect(('203.0.113.17', 443))
"""
    result = _pytest(tmp_path, source, "-p", "pytest_socket", "--allow-hosts=127.0.0.1", "--allow-unix-socket")
    assert result.returncode == 0, result.stdout + result.stderr


def test_reordering_is_reproducible_without_dropping_cases(tmp_path: Path) -> None:
    source = "\n".join(f"def test_case_{index}(): pass" for index in range(8))
    selected = []
    for seed in [13, 13, 14]:
        result = _pytest(tmp_path, source, "--integration-seed", str(seed))
        assert result.returncode == 0, result.stdout + result.stderr
        selected.append(json.loads((tmp_path / "reports/execution-main.json").read_text())["selected"])
    assert selected[0] == selected[1] and selected[0] != selected[2]
    assert set(selected[0]) == set(selected[2])
