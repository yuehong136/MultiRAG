"""SSH configuration, trust, bounded transport, and exact ownership failures."""

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import paramiko
import pytest

from agent.sandbox.providers.base import SandboxProviderConfigError
from agent.sandbox.providers.ssh import SSHProvider


@pytest.mark.parametrize(
    "key,value",
    [
        ("port", True),
        ("port", 0),
        ("port", 65536),
        ("timeout", 1.1),
        ("timeout", "1"),
        ("max_artifacts", -1),
        ("max_artifacts", False),
        ("work_dir", "/tmp/../etc"),
        ("work_dir", "tmp"),
        ("host", None),
        ("username", ""),
        ("unknown", "x"),
        ("private_key", "key"),
    ],
)
def test_invalid_config_never_connects(monkeypatch: pytest.MonkeyPatch, key: str, value: Any) -> None:
    provider = SSHProvider()
    monkeypatch.setattr(provider, "_create_ssh_client", lambda: pytest.fail("Invalid configuration connected"))
    with pytest.raises(SandboxProviderConfigError):
        provider.initialize({"host": "owned.invalid", "username": "runner", "password": "test-password", key: value})


def test_zero_artifact_count_is_preserved() -> None:
    provider = SSHProvider()
    assert provider.validate_config({"host": "owned.invalid", "username": "runner", "password": "test", "max_artifacts": 0}) == (True, None)


def test_supported_key_path_and_pem_loaders(tmp_path: Path) -> None:
    key = paramiko.RSAKey.generate(2048)
    path = tmp_path / "owned-key"
    key.write_private_key_file(str(path), password="owned-passphrase")
    provider = SSHProvider()
    provider.config = {"private_key": str(path), "passphrase": "owned-passphrase"}
    assert provider._load_private_key().get_base64() == key.get_base64()
    provider.config["private_key"] = path.read_text()
    assert provider._load_private_key().get_base64() == key.get_base64()
    assert not hasattr(paramiko, "DSSKey")


@pytest.mark.parametrize("unreadable", [False, True])
def test_trust_is_loaded_before_connection_and_fails_closed(monkeypatch: pytest.MonkeyPatch, unreadable: bool) -> None:
    calls: list[str] = []

    class Client:
        def load_system_host_keys(self) -> None:
            calls.append("system")

        def load_host_keys(self, path: str) -> None:
            calls.append("explicit")
            if unreadable:
                raise OSError("not readable")

        def set_missing_host_key_policy(self, policy: Any) -> None:
            assert isinstance(policy, paramiko.RejectPolicy)
            calls.append("reject")

        def connect(self, **kwargs: Any) -> None:
            assert not kwargs["allow_agent"] and not kwargs["look_for_keys"]
            calls.append("connect")

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(paramiko, "SSHClient", Client)
    provider = SSHProvider()
    provider.config = {"host": "owned.invalid", "username": "runner", "password": "secret", "port": 22, "known_hosts": "/owned/known_hosts", "timeout": 5}
    if unreadable:
        with pytest.raises(SandboxProviderConfigError):
            provider._create_ssh_client()
        assert calls == ["system", "explicit", "close"]
    else:
        provider._create_ssh_client()
        assert calls == ["system", "explicit", "reject", "connect"]


def test_transport_bounds_while_channel_is_running() -> None:
    closed: list[str] = []

    class Channel:
        def shutdown_write(self) -> None:
            pass

        def recv_ready(self) -> bool:
            return True

        def recv(self, size: int) -> bytes:
            return b"x" * size

        def recv_stderr_ready(self) -> bool:
            return False

        def recv_stderr(self, size: int) -> bytes:
            return b""

        def exit_status_ready(self) -> bool:
            return False

        def close(self) -> None:
            closed.append("channel")

    channel = Channel()
    stdout = SimpleNamespace(channel=channel, close=lambda: closed.append("stdout"))
    client = SimpleNamespace(exec_command=lambda *args, **kwargs: (io.BytesIO(), stdout, io.BytesIO()))
    with pytest.raises(RuntimeError, match="size limit"):
        SSHProvider._run_command(client, "owned-command", 1, max_bytes=1024)
    assert closed == ["stdout", "channel"]


def test_failed_mkdir_does_not_delete_preexisting_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    class Sftp:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def normalize(self, path: str) -> str:
            return "/owned"

        def mkdir(self, path: str, mode: int) -> None:
            raise FileExistsError(path)

    closed: list[bool] = []
    provider = SSHProvider()
    provider._initialized = True
    provider.config = {"work_dir": "/owned"}
    monkeypatch.setattr(provider, "_create_ssh_client", lambda: SimpleNamespace(open_sftp=Sftp, close=lambda: closed.append(True)))
    monkeypatch.setattr(provider, "_remove_workspace", lambda *args: pytest.fail("Deleted an unowned workspace"))
    with pytest.raises(FileExistsError):
        provider.create_instance()
    assert closed == [True]
