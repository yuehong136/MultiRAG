#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import base64
import json
import mimetypes
import os
import shutil
import signal
import selectors
import stat
import sys
import subprocess
import time
import uuid
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any

from agent.sandbox.result_protocol import build_javascript_wrapper, build_python_wrapper, extract_structured_result
from .base import ExecutionResult, SandboxInstance, SandboxProvider, SandboxProviderConfigError


ALLOWED_ARTIFACT_EXTENSIONS = {
    ".csv",
    ".html",
    ".jpeg",
    ".jpg",
    ".json",
    ".pdf",
    ".png",
    ".svg",
}


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


class LocalProvider(SandboxProvider):
    """
    Execute code as a local child process.

    This provider is intentionally gated by SANDBOX_LOCAL_ENABLED because it is
    not a sandbox boundary. Use a low-privilege runtime account.
    """

    def __init__(self) -> None:
        self.python_bin = "python3"
        self.node_bin = "node"
        self.work_dir = Path("/tmp/multirag-codeexec")
        self.timeout = 30
        self.max_memory_mb = 512
        self.max_output_bytes = 1024 * 1024
        self.max_artifacts = 20
        self.max_artifact_bytes = 10 * 1024 * 1024
        self._initialized = False
        self._instances: dict[str, Path] = {}

    def initialize(self, config: dict[str, Any]) -> bool:
        if not _env_enabled("SANDBOX_LOCAL_ENABLED"):
            raise SandboxProviderConfigError("Local code execution is disabled. Set SANDBOX_LOCAL_ENABLED=true to enable it.")

        if os.name != "posix":
            raise SandboxProviderConfigError("Local execution requires POSIX process groups and file descriptors.")

        try:
            self.python_bin = str(self._resolve_config_value(config, "python_bin", "SANDBOX_LOCAL_PYTHON_BIN", "python3"))
            self.node_bin = str(self._resolve_config_value(config, "node_bin", "SANDBOX_LOCAL_NODE_BIN", "node"))
            self.work_dir = Path(self._resolve_config_value(config, "work_dir", "SANDBOX_LOCAL_WORK_DIR", "/tmp/multirag-codeexec")).resolve()
            self.timeout = int(self._resolve_config_value(config, "timeout", "SANDBOX_LOCAL_TIMEOUT", 30))
            self.max_memory_mb = int(self._resolve_config_value(config, "max_memory_mb", "SANDBOX_LOCAL_MAX_MEMORY_MB", 512))
            self.max_output_bytes = int(self._resolve_config_value(config, "max_output_bytes", "SANDBOX_LOCAL_MAX_OUTPUT_BYTES", 1024 * 1024))
            self.max_artifacts = int(self._resolve_config_value(config, "max_artifacts", "SANDBOX_LOCAL_MAX_ARTIFACTS", 20))
            self.max_artifact_bytes = int(self._resolve_config_value(config, "max_artifact_bytes", "SANDBOX_LOCAL_MAX_ARTIFACT_BYTES", 10 * 1024 * 1024))
        except (TypeError, ValueError) as exc:
            raise SandboxProviderConfigError("Invalid local execution configuration.") from exc

        self._validate_limits()
        self.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialized = True
        return True

    def create_instance(self, template: str = "python") -> SandboxInstance:
        if not self._initialized:
            raise RuntimeError("Provider not initialized. Call initialize() first.")

        language = self._normalize_language(template)
        instance_id = str(uuid.uuid4())
        instance_dir = self.work_dir / instance_id
        instance_dir.mkdir(mode=0o700)
        (instance_dir / "artifacts").mkdir(mode=0o700)
        self._instances[instance_id] = instance_dir

        return SandboxInstance(
            instance_id=instance_id,
            provider="local",
            status="running",
            metadata={"language": language, "work_dir": str(instance_dir)},
        )

    def execute_code(
        self,
        instance_id: str,
        code: str,
        language: str,
        timeout: int = 10,
        arguments: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        if not self._initialized:
            raise RuntimeError("Provider not initialized. Call initialize() first.")

        normalized_lang = self._normalize_language(language)
        instance_dir = self._instances[instance_id]
        if instance_dir.is_symlink() or instance_dir.resolve() != instance_dir:
            raise RuntimeError("Local execution directory was replaced.")
        args_json = json.dumps(arguments or {}, ensure_ascii=False)
        command, script_path = self._prepare_script(instance_dir, normalized_lang, code, args_json)
        requested_timeout = self.timeout if timeout is None else int(timeout)
        if requested_timeout <= 0:
            raise RuntimeError(f"Execution timeout must be greater than 0 seconds, got {requested_timeout}.")
        exec_timeout = min(requested_timeout, self.timeout)

        start_time = time.monotonic()
        process = subprocess.Popen(
            self._build_limited_command(instance_dir, command, normalized_lang),
            cwd=instance_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._build_child_env(instance_dir),
            start_new_session=True,
        )
        try:
            stdout, stderr = self._read_output(process, exec_timeout)
        finally:
            # Also reap background children when main() returns successfully.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

        execution_time = time.monotonic() - start_time
        stdout, structured_result = extract_structured_result(stdout)
        if process.returncode == 0 and structured_result.get("present") is not True:
            raise RuntimeError("Local main() did not return a structured result.")

        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=process.returncode,
            execution_time=execution_time,
            metadata={
                "instance_id": instance_id,
                "language": normalized_lang,
                "script_path": str(script_path),
                "status": "ok" if process.returncode == 0 else "error",
                "timeout": exec_timeout,
                "artifacts": self._collect_artifacts(instance_dir / "artifacts"),
                "result_present": structured_result.get("present", False),
                "result_value": structured_result.get("value"),
                "result_type": structured_result.get("type"),
            },
        )

    def destroy_instance(self, instance_id: str) -> bool:
        if not self._initialized:
            raise RuntimeError("Provider not initialized. Call initialize() first.")

        instance_dir = self._instances.pop(instance_id, None)
        if instance_dir is None:
            return False
        if instance_dir.is_symlink():
            instance_dir.unlink()
        elif instance_dir.exists():
            shutil.rmtree(instance_dir)
        return True

    def health_check(self) -> bool:
        return self._initialized and self.work_dir.exists() and os.access(self.work_dir, os.W_OK)

    def get_supported_languages(self) -> list[str]:
        return ["python", "javascript", "nodejs"]

    @staticmethod
    def get_config_schema() -> dict[str, dict[str, Any]]:
        return {
            "python_bin": {"type": "string", "required": False, "default": "python3"},
            "node_bin": {"type": "string", "required": False, "default": "node"},
            "work_dir": {"type": "string", "required": False, "default": "/tmp/multirag-codeexec"},
            "timeout": {"type": "integer", "required": False, "default": 30},
            "max_memory_mb": {"type": "integer", "required": False, "default": 512},
            "max_output_bytes": {"type": "integer", "required": False, "default": 1048576},
            "max_artifacts": {"type": "integer", "required": False, "default": 20},
            "max_artifact_bytes": {"type": "integer", "required": False, "default": 10485760},
        }

    def _validate_limits(self) -> None:
        if self.timeout <= 0:
            raise SandboxProviderConfigError("SANDBOX_LOCAL_TIMEOUT must be greater than 0.")
        if self.max_memory_mb <= 0:
            raise SandboxProviderConfigError("SANDBOX_LOCAL_MAX_MEMORY_MB must be greater than 0.")
        if self.max_output_bytes <= 0:
            raise SandboxProviderConfigError("SANDBOX_LOCAL_MAX_OUTPUT_BYTES must be greater than 0.")
        if self.max_artifacts < 0:
            raise SandboxProviderConfigError("SANDBOX_LOCAL_MAX_ARTIFACTS must be greater than or equal to 0.")
        if self.max_artifact_bytes <= 0:
            raise SandboxProviderConfigError("SANDBOX_LOCAL_MAX_ARTIFACT_BYTES must be greater than 0.")

    def _prepare_script(self, instance_dir: Path, language: str, code: str, args_json: str) -> tuple[list[str], Path]:
        if language == "python":
            script_path = instance_dir / "main.py"
            script_path.write_text(build_python_wrapper(code, args_json), encoding="utf-8")
            return [self.python_bin, str(script_path)], script_path
        if language in {"javascript", "nodejs"}:
            script_path = instance_dir / "main.js"
            script_path.write_text(build_javascript_wrapper(code, args_json), encoding="utf-8")
            return [self.node_bin, f"--max-old-space-size={self.max_memory_mb}", str(script_path)], script_path
        raise RuntimeError(f"Unsupported language for local provider: {language}")

    @staticmethod
    def _resolve_config_value(config: dict[str, Any], key: str, env_name: str, default: Any) -> Any:
        return os.environ.get(env_name, config.get(key, default))

    def _build_child_env(self, instance_dir: Path) -> dict[str, str]:
        env = {
            "HOME": str(instance_dir),
            "MPLBACKEND": "Agg",
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
            "TMPDIR": str(instance_dir),
        }
        for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            if name in os.environ:
                env[name] = os.environ[name]
        return env

    def _build_limited_command(self, instance_dir: Path, command: list[str], language: str) -> list[str]:
        # Apply limits in a fresh interpreter, never preexec_fn in a threaded server.
        launcher = instance_dir / "launcher.py"
        launcher.write_text(
            "import os, resource, sys\n"
            "def limit(kind, value):\n"
            "    hard = resource.getrlimit(kind)[1]\n"
            "    value = value if hard == resource.RLIM_INFINITY else min(value, hard)\n"
            "    resource.setrlimit(kind, (value, value))\n"
            f"limit(resource.RLIMIT_CPU, {self.timeout + 1})\n"
            f"limit(resource.RLIMIT_FSIZE, {self.max_artifact_bytes})\n"
            "limit(resource.RLIMIT_NOFILE, 64)\n"
            # Darwin does not enforce RLIMIT_AS. Node needs virtual reservations
            # larger than its heap; its heap limit is supplied on the command line.
            + (f"limit(resource.RLIMIT_AS, {self.max_memory_mb * 1024 * 1024})\n" if sys.platform != "darwin" and language == "python" else "")
            + f"command = {command!r}\nos.execvpe(command[0], command, os.environ)\n",
            encoding="utf-8",
        )
        return [sys.executable, "-I", "-B", str(launcher)]

    def _read_output(self, process: subprocess.Popen[bytes], timeout: int) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        output = [bytearray(), bytearray()]
        total = 0
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((process.stdout, process.stderr)):
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map() or process.poll() is None:
                if process.poll() is not None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Execution timed out after {timeout} seconds")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, min(65536, self.max_output_bytes - total + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > self.max_output_bytes:
                        raise RuntimeError(f"Local execution output exceeded {self.max_output_bytes} bytes.")
                    output[key.data].extend(chunk)
        return output[0].decode("utf-8", errors="replace"), output[1].decode("utf-8", errors="replace")

    def _collect_artifacts(self, artifacts_dir: Path) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        instance_dir = artifacts_dir.parent
        if artifacts_dir.name != "artifacts" or self._instances.get(instance_dir.name) != instance_dir:
            raise RuntimeError("Artifact directory does not belong to a local execution instance.")
        try:
            with ExitStack() as resources:
                directory_fd: int | None = None
                for directory in (self.work_dir, instance_dir.name, "artifacts"):
                    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                    resources.callback(os.close, directory_fd)
                assert directory_fd is not None
                self._collect_directory(directory_fd, "", artifacts)
        except OSError as exc:
            raise RuntimeError("Artifact paths must be regular files in the execution artifacts directory.") from exc
        return artifacts

    def _collect_directory(self, directory_fd: int, prefix: str, artifacts: list[dict[str, Any]]) -> None:
        for name in sorted(os.listdir(directory_fd)):
            relative_name = f"{prefix}{name}"
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    self._collect_directory(fd, relative_name + "/", artifacts)
                finally:
                    os.close(fd)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError(f"Artifact links and special files are not allowed: {relative_name}")
            if len(artifacts) >= self.max_artifacts:
                raise RuntimeError(f"Local execution produced more than {self.max_artifacts} artifacts.")
            if Path(name).suffix.lower() not in ALLOWED_ARTIFACT_EXTENSIONS:
                raise RuntimeError(f"Unsupported artifact type: {relative_name}")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as stream:
                opened_info = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened_info.st_mode) or opened_info.st_nlink != 1:
                    raise RuntimeError(f"Artifact links and special files are not allowed: {relative_name}")
                if opened_info.st_size > self.max_artifact_bytes:
                    raise RuntimeError(f"Artifact exceeds {self.max_artifact_bytes} bytes: {relative_name}")
                content = stream.read(self.max_artifact_bytes + 1)
            if len(content) > self.max_artifact_bytes:
                raise RuntimeError(f"Artifact exceeds {self.max_artifact_bytes} bytes: {relative_name}")
            artifacts.append(
                {
                    "name": relative_name,
                    "content_b64": base64.b64encode(content).decode("ascii"),
                    "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                    "size": len(content),
                }
            )

    @staticmethod
    def _normalize_language(language: str) -> str:
        lang_lower = (language or "python").lower()
        if lang_lower in {"python", "python3"}:
            return "python"
        if lang_lower in {"javascript", "nodejs"}:
            return "nodejs"
        return lang_lower
