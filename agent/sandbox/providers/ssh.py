"""Execute on an explicitly trusted POSIX SSH host, with verified host keys."""

from __future__ import annotations

import io
import json
import posixpath
import shlex
import stat
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import ExecutionResult, SandboxInstance, SandboxProvider, SandboxProviderConfigError
from .local import LocalProvider

if TYPE_CHECKING:
    import paramiko


@dataclass
class _RemoteInstance:
    client: paramiko.SSHClient
    workspace: str
    executed: bool = False


class SSHProvider(SandboxProvider):
    """SSH encrypts transport; generated code still runs as the remote account."""

    def __init__(self) -> None:
        self.config: dict[str, Any] = {}
        self._initialized = False
        self._instances: dict[str, _RemoteInstance] = {}

    @staticmethod
    def get_config_schema() -> dict[str, dict[str, Any]]:
        schema = {
            "host": {"type": "string", "required": True, "label": "SSH host"},
            "port": {"type": "integer", "default": 22, "min": 1, "max": 65535, "label": "SSH port"},
            "username": {"type": "string", "required": True, "label": "SSH username"},
            "password": {"type": "string", "secret": True, "label": "SSH password"},
            "private_key": {"type": "string", "secret": True, "multiline": True, "label": "SSH private key", "description": "OpenSSH/PEM contents or a readable key file on the MultiRAG server."},
            "passphrase": {"type": "string", "secret": True, "label": "Key passphrase"},
            "known_hosts": {
                "type": "string",
                "default": "",
                "label": "Known hosts file",
                "description": "Server-side OpenSSH known_hosts path. System host keys are also loaded; unknown hosts are rejected.",
            },
        }
        schema.update({key: dict(value) for key, value in LocalProvider.get_config_schema().items() if not value.get("readonly")})
        schema["work_dir"].update(default="/tmp", label="Remote workspace root")
        for value in schema.values():
            value["scope"] = "runtime"
        return schema

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str | None]:
        schema = self.get_config_schema()
        if set(config) - set(schema):
            return False, "Unknown SSH configuration field."
        for key, field in schema.items():
            value = config.get(key, field.get("default"))
            if field.get("required") and (not isinstance(value, str) or not value.strip()):
                return False, f"{key} is required."
            if value is None and key not in config:
                continue
            expected = str if field["type"] == "string" else int
            if type(value) is not expected:
                return False, f"{key} must be {field['type']}."
            if expected is int and not field["min"] <= value <= field["max"]:
                return False, f"{key} is out of range."
        password, key = config.get("password", ""), config.get("private_key", "")
        if bool(password) == bool(key):
            return False, "Choose exactly one SSH authentication method: password or private_key."
        if not key and config.get("passphrase"):
            return False, "A passphrase requires a private key."
        for name in ("python_bin", "node_bin", "work_dir"):
            value = config.get(name, schema[name]["default"])
            if not value.strip() or "\x00" in value:
                return False, f"{name} must not be empty."
        work_dir = config.get("work_dir", "/tmp")
        if not work_dir.startswith("/") or posixpath.normpath(work_dir) != work_dir.rstrip("/"):
            return False, "work_dir must be an absolute normalized POSIX directory."
        return True, None

    def initialize(self, config: dict[str, Any]) -> bool:
        valid, error = self.validate_config(config)
        if not valid:
            raise SandboxProviderConfigError(error)
        self.config = {key: field["default"] for key, field in self.get_config_schema().items() if "default" in field}
        self.config.update(config)
        client = self._create_ssh_client()
        try:
            # Confirm a POSIX Python runtime before publishing the provider.
            self._run_command(client, self._python_command("-c", "import os; assert os.name == 'posix'"), 10)
        finally:
            client.close()
        self._initialized = True
        return True

    def _python_command(self, *args: str) -> str:
        return shlex.join([self.config["python_bin"], "-I", "-B", *args])

    def _create_ssh_client(self) -> paramiko.SSHClient:
        import paramiko

        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
            if self.config.get("known_hosts"):
                client.load_host_keys(self.config["known_hosts"])
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            options = {
                "hostname": self.config["host"],
                "port": self.config["port"],
                "username": self.config["username"],
                "timeout": min(self.config["timeout"], 10),
                "banner_timeout": 10,
                "auth_timeout": 10,
                "look_for_keys": False,
                "allow_agent": False,
            }
            if self.config.get("private_key"):
                options["pkey"] = self._load_private_key()
            else:
                options["password"] = self.config["password"]
            client.connect(**options)
            return client
        except Exception as exc:
            client.close()
            # Authentication/key-loader errors can contain secret input. Keep it server-side.
            raise SandboxProviderConfigError("SSH connection failed. Check credentials, runtime and trusted host keys.") from exc

    def _load_private_key(self) -> paramiko.PKey:
        import paramiko

        value = self.config["private_key"].strip()
        passphrase = self.config.get("passphrase") or None
        is_path = "\n" not in value and not value.startswith("-----BEGIN")
        # Use supported key classes, including their passphrase-aware loaders.
        # DSSKey was removed in current Paramiko releases.
        for key_class in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                if is_path:
                    return key_class.from_private_key_file(value, password=passphrase)
                return key_class.from_private_key(io.StringIO(value), password=passphrase)
            except (ValueError, OSError, paramiko.SSHException):
                continue
        raise SandboxProviderConfigError("Unable to load SSH private key.")

    @staticmethod
    def _runner_bundle() -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for package in ("agent", "agent/sandbox", "agent/sandbox/providers"):
                archive.writestr(package + "/__init__.py", "")
            root = Path(__file__).parent
            for name in ("base.py", "process.py", "ssh_runner.py"):
                archive.writestr("agent/sandbox/providers/" + name, (root / name).read_bytes())
            archive.writestr("agent/sandbox/result_protocol.py", (root.parent / "result_protocol.py").read_bytes())
            archive.writestr("__main__.py", "from agent.sandbox.providers.ssh_runner import main\nmain()\n")
        return output.getvalue()

    def create_instance(self, template: str = "python") -> SandboxInstance:
        if not self._initialized:
            raise RuntimeError("SSH provider is not initialized.")
        if template.lower() not in {"python", "python3", "javascript", "nodejs"}:
            raise ValueError("Unsupported SSH language.")
        instance_id = uuid.uuid4().hex
        client = self._create_ssh_client()
        workspace: str | None = None
        created_workspace = False
        try:
            with client.open_sftp() as sftp:
                base = sftp.normalize(self.config["work_dir"])
                workspace = posixpath.join(base, "multirag-codeexec-" + instance_id)
                sftp.mkdir(workspace, mode=0o700)
                created_workspace = True
                if not stat.S_ISDIR(sftp.lstat(workspace).st_mode or 0):
                    raise RuntimeError("SSH workspace is not a directory.")
                with sftp.file(posixpath.join(workspace, ".lock"), "wx"):
                    pass
                with sftp.file(posixpath.join(workspace, "runner.pyz"), "wx") as stream:
                    stream.write(self._runner_bundle())
            self._instances[instance_id] = _RemoteInstance(client, workspace)
        except Exception:
            try:
                if created_workspace and workspace is not None:
                    self._remove_workspace(client, workspace)
            finally:
                client.close()
            raise
        return SandboxInstance(instance_id, "ssh", "running", {"language": template, "remote_work_dir": workspace})

    def execute_code(self, instance_id: str, code: str, language: str, timeout: int = 10, arguments: dict[str, Any] | None = None) -> ExecutionResult:
        instance = self._instances[instance_id]
        if instance.executed:
            raise RuntimeError("SSH instances support one execution.")
        if type(timeout) is not int or timeout < 1:
            raise ValueError("Execution timeout must be a positive integer.")
        if arguments is not None and not isinstance(arguments, dict):
            raise TypeError("Code execution arguments must be an object.")
        runtime = {key: self.config[key] for key, field in LocalProvider.get_config_schema().items() if not field.get("readonly") and key != "work_dir"}
        request = json.dumps(
            {"code": code, "language": language, "arguments": arguments if arguments is not None else {}, "timeout": min(timeout, self.config["timeout"]), "config": runtime},
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        if len(request) > 8 * 1024 * 1024:
            raise ValueError("SSH execution request exceeds 8 MiB.")
        instance.executed = True
        wire_limit = 2 * self.config["max_output_bytes"] + 90 * 1024 * 1024
        body = self._run_command(
            instance.client, self._python_command(posixpath.join(instance.workspace, "runner.pyz"), instance.workspace), min(timeout, self.config["timeout"]) + 10, request, wire_limit
        )
        try:
            payload = json.loads(body)
            if "error" in payload:
                error_class = TimeoutError if payload.get("error_type") == "TimeoutError" else RuntimeError
                raise error_class(payload["error"])
            return ExecutionResult(**payload["result"])
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("Invalid SSH execution response.") from exc

    @staticmethod
    def _run_command(client: paramiko.SSHClient, command: str, timeout: int, payload: bytes = b"", max_bytes: int = 1024 * 1024) -> str:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        channel = stdout.channel
        try:
            stdin.write(payload)
            stdin.flush()
            channel.shutdown_write()
            output = [bytearray(), bytearray()]
            total = 0
            deadline = time.monotonic() + timeout
            while True:
                for index, ready, receive in ((0, channel.recv_ready, channel.recv), (1, channel.recv_stderr_ready, channel.recv_stderr)):
                    if ready():
                        chunk = receive(min(65536, max_bytes - total + 1))
                        total += len(chunk)
                        if total > max_bytes:
                            raise RuntimeError("SSH response exceeded its size limit.")
                        output[index].extend(chunk)
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("SSH transport timed out; remote execution retains its own deadline and cleanup.")
                time.sleep(0.01)
            if channel.recv_exit_status() != 0:
                raise RuntimeError("SSH remote runtime command failed.")
            return output[0].decode("utf-8")
        finally:
            stdin.close()
            stdout.close()
            stderr.close()
            channel.close()

    def _remove_workspace(self, client: paramiko.SSHClient, workspace: str) -> None:
        # Only a path constructed by create_instance is eligible. rmtree uses
        # descriptor-relative deletion on POSIX and does not follow symlinks.
        name = posixpath.basename(workspace)
        if not name.startswith("multirag-codeexec-") or len(name) != len("multirag-codeexec-") + 32:
            raise RuntimeError("Invalid SSH cleanup workspace.")
        script = """import fcntl, os, shutil, sys
p = sys.argv[1]
if os.path.islink(p):
    os.unlink(p)
elif os.path.exists(p):
    try:
        lock = open(os.path.join(p, '.lock'), 'rb')
    except FileNotFoundError:
        try:
            shutil.rmtree(p)
        except FileNotFoundError:
            pass
    else:
        with lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if os.path.exists(p):
                shutil.rmtree(p)
"""
        self._run_command(client, self._python_command("-c", script, workspace), self.config["timeout"] + 10)

    def destroy_instance(self, instance_id: str) -> bool:
        instance = self._instances.get(instance_id)
        if instance is None:
            return False
        transport = instance.client.get_transport()
        if transport is None or not transport.is_active():
            # A lost execution channel must not make cleanup permanently
            # unretryable; retain ownership until a fresh connection confirms it.
            instance.client = self._create_ssh_client()
        try:
            self._remove_workspace(instance.client, instance.workspace)
        finally:
            instance.client.close()
        del self._instances[instance_id]
        return True

    def health_check(self) -> bool:
        try:
            client = self._create_ssh_client()
            try:
                self._run_command(client, self._python_command("-c", "import os; assert os.name == 'posix'"), 10)
            finally:
                client.close()
            return True
        except Exception:
            return False

    def get_supported_languages(self) -> list[str]:
        return ["python", "javascript", "nodejs"]
