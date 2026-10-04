"""Regression tests for selection, owned services and fail-closed reporting."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.run_integration import main, selected_paths
from tests.support import services
from tests.support.integration_suites import DATABASE_TESTS, INTEGRATION, ROOT, required_services, suite_paths


def test_database_suite_never_requires_unrelated_services() -> None:
    paths = suite_paths("db")
    assert {path.name for path in paths} == DATABASE_TESTS
    assert required_services(paths) == {"postgresql"}
    assert required_services(suite_paths("infinity")) == {"infinity"}
    assert required_services([INTEGRATION / "new_test.py"]) == {"postgresql", "redis", "minio", "milvus"}


def test_explicit_node_selection_prepares_only_its_dependencies() -> None:
    paths = selected_paths("core", [str(INTEGRATION / "test_async_engine.py") + "::test_async_engine_select_one", "-q"])
    assert required_services(paths) == {"postgresql"}
    with pytest.raises(ValueError, match="under tests/integration"):
        selected_paths("core", [str(ROOT / "tests/unit/test_app_config.py")])
    with pytest.raises(ValueError, match="directory targets"):
        selected_paths("core", [str(INTEGRATION)])


def test_pytest_filter_and_ignore_values_are_not_mistaken_for_targets() -> None:
    assert selected_paths("db", ["-k", "api", "--ignore", str(INTEGRATION / "test_async_engine.py")]) == suite_paths("db")


@pytest.mark.parametrize("option", ["-nauto", "-n2", "--numprocesses=2"])
def test_parallel_options_cannot_bypass_storage_isolation_guard(option: str) -> None:
    with pytest.raises(SystemExit) as result:
        main(["--", str(INTEGRATION / "test_runtime_document_upload.py"), option])
    assert result.value.code == 2


def test_inherited_parallel_option_cannot_bypass_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-nauto")
    with pytest.raises(SystemExit) as result:
        main(["--", str(INTEGRATION / "test_runtime_document_upload.py")])
    assert result.value.code == 2


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
            return "127.0.0.1"

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
