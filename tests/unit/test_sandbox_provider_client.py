"""Provider selection and CodeExec errors never switch an explicit backend."""

import base64
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.sandbox import client
from agent.sandbox.providers import AliyunCodeInterpreterProvider
from agent.sandbox.providers.base import ExecutionResult, SandboxProviderConfigError, SandboxProviderNotConfigured
from agent.sandbox.result_protocol import RESULT_MARKER_PREFIX


@pytest.fixture(autouse=True)
def reset_provider(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(client, "_provider_manager", None)
    monkeypatch.delenv("SANDBOX_PROVIDER_TYPE", raising=False)
    yield


def test_environment_selection_and_field_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_PROVIDER_TYPE", "local")
    monkeypatch.setenv("SANDBOX_LOCAL_TIMEOUT", "2")
    monkeypatch.setattr(client, "_load_provider_config_from_settings", lambda _: {"timeout": 9, "max_artifacts": 3})
    assert client._resolve_provider_type() == ("local", True)
    assert client._load_provider_config("local") == {"timeout": "2", "max_artifacts": 3}


def test_settings_provider_selection_and_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "SessionLocal", lambda: nullcontext(object()))
    rows = {"sandbox.provider_type": [SimpleNamespace(value="e2b")], "sandbox.e2b": [SimpleNamespace(value='{"api_key":"test-key"}')]}
    monkeypatch.setattr(client.SystemSettingsService, "get_by_name", lambda _db, name: rows[name])
    assert client._resolve_provider_type() == ("e2b", False)
    assert client._load_provider_config("e2b") == {"api_key": "test-key"}


@pytest.mark.parametrize("value", ["[]", "null", "bad-json"])
def test_invalid_settings_fail_closed(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr(client, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(client.SystemSettingsService, "get_by_name", lambda *_: [SimpleNamespace(value=value)])
    with pytest.raises(SandboxProviderConfigError):
        client._load_provider_config("local", from_env=True)


def test_explicit_environment_can_run_without_settings_db(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_: str) -> Any:
        raise ConnectionError("test settings unavailable")

    monkeypatch.setattr(client, "_load_provider_config_from_settings", unavailable)
    monkeypatch.setenv("SANDBOX_LOCAL_TIMEOUT", "3")
    assert client._load_provider_config("local", from_env=True)["timeout"] == "3"
    with pytest.raises(ConnectionError):
        client._load_provider_config("local")


def test_self_managed_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_HOST", "executor.test")
    monkeypatch.setenv("SANDBOX_EXECUTOR_MANAGER_PORT", "1234")
    monkeypatch.setenv("SANDBOX_EXECUTOR_MANAGER_POOL_SIZE", "4")
    assert client._load_provider_config_from_env("self_managed") == {"endpoint": "http://executor.test:1234", "pool_size": "4"}


def test_bad_explicit_selection_does_not_poison_manager_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_PROVIDER_TYPE", "local")
    monkeypatch.delenv("SANDBOX_LOCAL_ENABLED", raising=False)
    monkeypatch.setattr(client, "_load_provider_config_from_settings", lambda _: {})
    for _ in range(2):
        with pytest.raises(SandboxProviderConfigError, match="disabled"):
            client.get_provider_manager()
        assert client._provider_manager is None
    monkeypatch.setenv("SANDBOX_PROVIDER_TYPE", "unknown-provider")
    with pytest.raises(SandboxProviderConfigError, match="Unknown"):
        client.get_provider_manager()


def test_client_executes_and_cleans_local_instance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sys

    monkeypatch.setenv("SANDBOX_PROVIDER_TYPE", "local")
    monkeypatch.setenv("SANDBOX_LOCAL_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_LOCAL_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("SANDBOX_LOCAL_PYTHON_BIN", sys.executable)
    monkeypatch.setattr(client, "_load_provider_config_from_settings", lambda _: {})
    assert client.execute_code('def main(): return {"ok": True}').metadata["result_value"] == {"ok": True}
    assert list(tmp_path.iterdir()) == []
    client.reload_provider()
    assert client._provider_manager is None


def test_no_provider_is_the_only_fallback_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(client.SystemSettingsService, "get_by_name", lambda *_: [])
    with pytest.raises(SandboxProviderNotConfigured):
        client.get_provider_manager()


@pytest.mark.parametrize("failure", [SandboxProviderConfigError("disabled"), RuntimeError("provider failed"), TimeoutError("timed out"), ImportError("provider dependency missing")])
def test_codeexec_does_not_fallback_on_provider_errors(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    import requests

    from agent.tools.code_exec import CodeExec

    def fail(**_: Any) -> Any:
        raise failure

    monkeypatch.setattr(client, "execute_code", fail)
    monkeypatch.setattr(requests, "post", lambda **_: pytest.fail("Unexpected HTTP fallback"))
    tool = CodeExec.__new__(CodeExec)
    tool._param = SimpleNamespace(outputs={"result": {"value": "stale", "type": "Any"}, "attachments": {"value": ["stale"]}})
    tool._canvas = SimpleNamespace(is_canceled=lambda: False)
    output = tool._execute_code("python", "def main(): return 1", {})
    assert str(failure) in output["_ERROR"]
    assert output["result"] is None
    assert output["attachments"] == []


def test_nonzero_provider_result_is_not_business_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.tools.code_exec import CodeExec

    monkeypatch.setattr(client, "execute_code", lambda **_: ExecutionResult('{"fake": true}', "failure", 1, 0.1, {"result_present": True, "result_value": {"fake": True}}))
    tool = CodeExec.__new__(CodeExec)
    tool._param = SimpleNamespace(outputs={"result": {"value": "stale", "type": "Any"}})
    tool._canvas = SimpleNamespace(is_canceled=lambda: False)
    output = tool._execute_code("python", "def main(): return 1", {})
    assert output["_ERROR"] == "failure"
    assert output["result"] is None


@pytest.mark.parametrize("failure", ["stderr", "empty_http", "http_exception", "canceled_before_provider", "canceled_after_provider", "canceled_after_http"])
def test_failed_execution_clears_all_previous_outputs(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    import requests

    from agent.tools.code_exec import CodeExec

    calls = 0

    def canceled() -> bool:
        nonlocal calls
        calls += 1
        return calls == {"canceled_before_provider": 1, "canceled_after_provider": 3, "canceled_after_http": 4}.get(failure)

    def execute(**_: Any) -> ExecutionResult:
        if failure in {"empty_http", "http_exception", "canceled_after_http"}:
            raise SandboxProviderNotConfigured("no provider selected")
        return ExecutionResult("", "execution failed", 0, 0.1, {})

    def post(**_: Any) -> SimpleNamespace:
        if failure == "http_exception":
            raise requests.ConnectionError("test executor unavailable")
        return SimpleNamespace(status_code=200, json=lambda: {})

    monkeypatch.setattr(client, "execute_code", execute)
    monkeypatch.setattr(requests, "post", post)
    tool = CodeExec.__new__(CodeExec)
    tool._param = SimpleNamespace(outputs={name: {"value": "stale", "type": "Any"} for name in ("result", "attachments", "_ARTIFACTS", "_ATTACHMENT_CONTENT", "content", "raw_result", "actual_type")})
    tool._canvas = SimpleNamespace(is_canceled=canceled)

    output = tool._execute_code("python", "def main(): return 42", {})

    assert output["_ERROR"]
    assert output["result"] is None
    assert output["raw_result"] is None
    assert output["_ARTIFACTS"] is None
    assert output["attachments"] == []
    assert output["_ATTACHMENT_CONTENT"] == ""
    assert output["content"] == ""
    assert output["actual_type"] == ""


@pytest.mark.parametrize("exit_code", [None, 1])
def test_codeexec_keeps_legacy_http_when_no_provider_selected(monkeypatch: pytest.MonkeyPatch, exit_code: int | None) -> None:
    import requests

    from agent.tools.code_exec import CodeExec

    def absent(**_: Any) -> Any:
        raise SandboxProviderNotConfigured("none selected")

    body = {"stdout": "", "stderr": "", "result": {"present": True, "value": 42, "type": "json"}}
    if exit_code is not None:
        body["exit_code"] = exit_code
    monkeypatch.setattr(client, "execute_code", absent)
    monkeypatch.setattr(requests, "post", lambda **_: SimpleNamespace(status_code=200, json=lambda: body))
    tool = CodeExec.__new__(CodeExec)
    tool._param = SimpleNamespace(outputs={"result": {"value": None, "type": "Number"}})
    tool._canvas = SimpleNamespace(is_canceled=lambda: False)
    output = tool._execute_code("python", "def main(): return 42", {})
    if exit_code:
        assert output["result"] is None
        assert "exited with code 1" in output["_ERROR"]
    else:
        assert output["result"] == 42


def test_aliyun_uses_shared_result_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.sandbox.providers import aliyun_codeinterpreter as aliyun

    marker = RESULT_MARKER_PREFIX + base64.b64encode(json.dumps({"present": True, "value": False, "type": "json"}).encode()).decode()
    captured: dict[str, Any] = {}

    def execute(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"results": [{"type": "stdout", "text": marker}, {"type": "endOfExecution", "status": "ok"}]}

    monkeypatch.setattr(aliyun.Sandbox, "connect", lambda **_: SimpleNamespace(context=SimpleNamespace(execute=execute)))
    provider = AliyunCodeInterpreterProvider()
    provider._initialized = True
    provider._config = object()
    result = provider.execute_code("test-instance", "def main(flag): return flag", "python", arguments={"flag": False})
    assert result.metadata["result_value"] is False
    assert result.stdout == ""
    assert "json.loads(" in captured["code"]


def test_provider_module_import_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    import requests

    from agent.tools.code_exec import CodeExec

    original_import = builtins.__import__

    def unavailable(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "agent.sandbox.providers.base":
            raise ImportError("test provider module unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(requests, "post", lambda **_: pytest.fail("Unexpected HTTP fallback"))
    monkeypatch.setattr(builtins, "__import__", unavailable)
    tool = CodeExec.__new__(CodeExec)
    tool._param = SimpleNamespace(outputs={"result": {"value": "stale", "type": "Any"}})
    tool._canvas = SimpleNamespace(is_canceled=lambda: False)
    output = tool._execute_code("python", "def main(): return 1", {})
    assert "test provider module unavailable" in output["_ERROR"]
    assert output["result"] is None
