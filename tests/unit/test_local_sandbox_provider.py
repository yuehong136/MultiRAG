"""Harmless subprocess acceptance using only pytest-owned temporary directories."""

import base64
import json
import os
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent.sandbox.providers.base import SandboxProviderConfigError
from agent.sandbox.providers.local import LocalProvider
from agent.sandbox.result_protocol import RESULT_MARKER_PREFIX, extract_structured_result


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[LocalProvider]:
    for name in list(os.environ):
        if name.startswith("SANDBOX_LOCAL_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SANDBOX_LOCAL_ENABLED", "true")
    item = LocalProvider()
    item.initialize({"python_bin": sys.executable, "work_dir": str(tmp_path / "runs"), "timeout": 5})
    yield item
    for instance_id in list(item._instances):
        item.destroy_instance(instance_id)
    assert list(item.work_dir.iterdir()) == []


def run_code(provider: LocalProvider, code: str, language: str = "python", arguments: dict[str, Any] | None = None) -> Any:
    instance = provider.create_instance(language)
    try:
        return provider.execute_code(instance.instance_id, code, language, timeout=5, arguments=arguments)
    finally:
        provider.destroy_instance(instance.instance_id)


def test_disabled_requires_environment_even_if_config_says_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SANDBOX_LOCAL_ENABLED", raising=False)
    with pytest.raises(SandboxProviderConfigError, match="disabled"):
        LocalProvider().initialize({"enabled": True, "work_dir": str(tmp_path)})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("key,value", [("timeout", 0), ("max_memory_mb", -1), ("max_output_bytes", 0), ("max_artifacts", -1), ("max_artifact_bytes", 0), ("timeout", "bad")])
def test_invalid_configuration(provider: LocalProvider, key: str, value: Any) -> None:
    with pytest.raises(SandboxProviderConfigError):
        LocalProvider().initialize({"work_dir": str(provider.work_dir), key: value})


@pytest.mark.parametrize("value", [None, False, True, 42, [1, False, None], {"label": "测试'\\\n", "empty": None}])
def test_python_structured_values_and_json_arguments(provider: LocalProvider, value: Any) -> None:
    result = run_code(provider, 'def main(value):\n    print("log without newline", end="")\n    return value', arguments={"value": value})
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "log without newline\n"
    assert result.metadata["result_present"] is True
    assert result.metadata["result_value"] == value


@pytest.mark.parametrize("code", ['def main():\n    raise ValueError("harmless failure")', "def main():\n    return {1, 2}", 'def main():\n    return float("nan")'])
def test_python_failure_has_no_structured_success(provider: LocalProvider, code: str) -> None:
    result = run_code(provider, code)
    assert result.exit_code != 0
    assert result.stderr
    assert result.metadata["result_present"] is False


@pytest.mark.parametrize("code", ["async function main(args) { return args; }", "module.exports.main = async (args) => args;"])
def test_javascript_async_and_exported_main(provider: LocalProvider, code: str) -> None:
    assert shutil.which("node"), "Node is required for local provider acceptance"
    result = run_code(provider, code, "javascript", {"flag": False, "empty": None, "text": "测试"})
    assert result.exit_code == 0, result.stderr
    assert result.stdout == ""
    assert result.metadata["result_value"] == {"flag": False, "empty": None, "text": "测试"}


@pytest.mark.parametrize(
    "code",
    [
        'async function main() { throw new Error("harmless failure"); }',
        "function main() {}",
        "const unrelated = 1;",
        "function main() { return 1n; }",
        'function main() { return Symbol("not JSON"); }',
        "function main() { return () => 42; }",
    ],
)
def test_javascript_failure_is_nonzero(provider: LocalProvider, code: str) -> None:
    result = run_code(provider, code, "nodejs")
    assert result.exit_code != 0
    assert result.stderr
    assert result.metadata["result_present"] is False


@pytest.mark.parametrize("stderr", [False, True])
def test_output_is_bounded_during_execution(provider: LocalProvider, stderr: bool) -> None:
    provider.max_output_bytes = 2048
    stream = "sys.stderr" if stderr else "sys.stdout"
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="output exceeded"):
        run_code(provider, f'import sys\ndef main():\n    while True:\n        {stream}.write("x" * 1024)\n        {stream}.flush()')
    assert time.monotonic() - start < 3


@pytest.mark.parametrize("finish", [False, True])
def test_process_group_cleanup_after_timeout_and_success(provider: LocalProvider, tmp_path: Path, finish: bool) -> None:
    provider.timeout = 1 if not finish else 5
    marker = tmp_path / "should-not-exist"
    descendant = f"import time; from pathlib import Path; time.sleep(1.5); Path({str(marker)!r}).write_text('leaked')"
    code = f'import subprocess, sys, time\ndef main():\n    subprocess.Popen([sys.executable, "-c", {descendant!r}])\n'
    code += "    return True\n" if finish else "    time.sleep(10)\n    return True\n"
    if finish:
        assert run_code(provider, code).metadata["result_value"] is True
    else:
        with pytest.raises(TimeoutError):
            run_code(provider, code)
    time.sleep(1.6)
    assert not marker.exists(), "A background process survived the local execution group"


def test_artifact_bytes_and_nested_relative_path(provider: LocalProvider) -> None:
    result = run_code(provider, 'from pathlib import Path\ndef main():\n    Path("artifacts/nested").mkdir()\n    Path("artifacts/nested/data.json").write_text("{}")\n    return None')
    assert result.metadata["artifacts"] == [{"name": "nested/data.json", "content_b64": base64.b64encode(b"{}").decode(), "mime_type": "application/json", "size": 2}]


@pytest.mark.parametrize("kind", ["symlink", "root_symlink", "instance_symlink", "hardlink", "fifo", "extension", "count", "size"])
def test_artifact_limits_and_paths(provider: LocalProvider, tmp_path: Path, kind: str) -> None:
    instance = provider.create_instance()
    artifacts = provider.work_dir / instance.instance_id / "artifacts"
    outside = tmp_path / "outside.json"
    outside.write_text("harmless external content")
    if kind == "symlink":
        (artifacts / "link.json").symlink_to(outside)
    elif kind == "root_symlink":
        artifacts.rmdir()
        artifacts.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "instance_symlink":
        shutil.rmtree(artifacts.parent)
        (tmp_path / "artifacts").mkdir()
        (tmp_path / "artifacts" / "external.json").write_text("external")
        artifacts.parent.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "hardlink":
        os.link(outside, artifacts / "link.json")
    elif kind == "fifo":
        os.mkfifo(artifacts / "pipe.json")
    elif kind == "extension":
        (artifacts / "bad.exe").write_bytes(b"harmless")
    elif kind == "count":
        provider.max_artifacts = 0
        (artifacts / "data.json").write_bytes(b"{}")
    else:
        provider.max_artifact_bytes = 1
        (artifacts / "data.json").write_bytes(b"{}")
    with pytest.raises(RuntimeError):
        provider._collect_artifacts(artifacts)
    provider.destroy_instance(instance.instance_id)
    assert outside.read_text() == "harmless external content"


def test_replaced_instance_is_rejected_before_script_write(provider: LocalProvider, tmp_path: Path) -> None:
    instance = provider.create_instance()
    directory = provider.work_dir / instance.instance_id
    shutil.rmtree(directory)
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeError, match="replaced"):
        provider.execute_code(instance.instance_id, "def main(): return True", "python")
    assert not (tmp_path / "main.py").exists()
    provider.destroy_instance(instance.instance_id)
    assert tmp_path.is_dir()


def test_child_environment_does_not_inherit_credentials(provider: LocalProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-do-not-forward")
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "1")
    result = run_code(provider, 'import os\ndef main():\n    return {"secret": os.getenv("AWS_SECRET_ACCESS_KEY"), "threads": os.getenv("OPENBLAS_NUM_THREADS")}')
    assert result.metadata["result_value"] == {"secret": None, "threads": "1"}


@pytest.mark.parametrize("payload", [[], None, "text", {"present": True}, {"present": 1, "value": 3, "type": "json"}])
def test_malformed_result_envelopes_remain_stdout(payload: Any) -> None:
    line = RESULT_MARKER_PREFIX + base64.b64encode(json.dumps(payload).encode()).decode()
    assert extract_structured_result(line + "\n") == (line + "\n", {})


def test_invalid_base64_and_empty_marker_remain_stdout() -> None:
    stdout = f"{RESULT_MARKER_PREFIX}!invalid!\n{RESULT_MARKER_PREFIX}\n"
    assert extract_structured_result(stdout) == (stdout, {})


@pytest.mark.parametrize("language,code", [("python", 'import sys\ndef main():\n    print("fake log")\n    sys.exit(0)'), ("nodejs", 'function main() { console.log("fake log"); process.exit(0); }')])
def test_early_zero_exit_without_return_is_an_error(provider: LocalProvider, language: str, code: str) -> None:
    with pytest.raises(RuntimeError, match="did not return a structured result"):
        run_code(provider, code, language)
