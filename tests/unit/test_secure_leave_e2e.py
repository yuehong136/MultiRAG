from __future__ import annotations

import base64
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts import secure_leave_e2e as e2e


def _layout(tmp_path: Path) -> e2e.Layout:
    return e2e._ensure_layout((tmp_path / "deployment").resolve())


def _candidate() -> e2e.Candidate:
    return e2e.Candidate(
        ref="candidate-0123456789abcdef0123",
        tenant_id="tenant",
        platform_user_id="user",
        provider_tenant="provider-tenant",
        provider_account_id="provider-account",
        external_identity_id="identity",
        identity_revision=3,
        channel_id="channel",
        binding_id="binding",
        binding_generation=1,
        agent_id="agent",
        agent_revision_id="revision",
        mcp_server_id="server",
    )


@pytest.mark.parametrize(
    "dsl",
    [
        {
            "components": [
                {"component_name": "Agent", "params": {"mcp": [{"mcp_id": "server", "tools": {}}]}},
                {"component_name": "Agent", "params": {"mcp": [{"mcp_id": "other", "tools": {"leave_preview_leave_form": {}}}]}},
            ],
        },
        {"prompt": "server leave_preview_leave_form"},
        {"component_name": "Agent", "params": {"mcp": [{"mcp_id": "server", "tools": {"other_leave_preview_leave_form": {}}}]}},
        {"component_name": "Agent", "params": {"mcp": [{"mcp_id": "other", "tools": {"leave_preview_leave_form": {}}}]}},
    ],
)
def test_canvas_authority_rejects_cross_entry_or_unstructured_matches(
    dsl: object,
) -> None:
    assert not e2e._contains_canvas_authority(dsl, server_id="server")


def test_canvas_authority_requires_exact_tool_on_selected_mcp_entry() -> None:
    dsl = {
        "obj": {
            "component_name": "Agent",
            "params": {
                "mcp": [
                    {
                        "mcp_id": "server",
                        "tools": {"leave_preview_leave_form": {"description": "preview"}},
                    },
                ],
            },
        },
    }
    assert e2e._contains_canvas_authority(dsl, server_id="server")


def _manifest(tmp_path: Path) -> dict[str, Any]:
    return {
        "ports": dict(e2e.DEFAULT_PORTS),
        "urls": {
            "api": "http://127.0.0.1:8123",
            "gateway": "https://127.0.0.1:8765/mcp",
            "issuer": "https://127.0.0.1:9277",
            "jwks": "https://127.0.0.1:9277/.well-known/jwks.json",
            "mock": "http://127.0.0.1:18765",
        },
        "repositories": {
            "multirag": str(e2e.REPOSITORY_ROOT),
            "ofmcp": str(tmp_path / "ofmcp"),
        },
    }


def test_documented_file_entrypoint_inserts_repository_import_path() -> None:
    original = list(e2e.sys.path)
    repository = str(e2e.REPOSITORY_ROOT)
    try:
        e2e.sys.path[:] = [item for item in e2e.sys.path if item != repository]
        e2e._ensure_repository_import_path()
        assert e2e.sys.path[0] == repository
    finally:
        e2e.sys.path[:] = original


def test_fetch_json_disables_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return b'{"status":"ok"}'

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Opener:
        def open(self, request: object, *, timeout: float) -> Response:
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    def build_opener(*handlers: object) -> Opener:
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(e2e.urllib.request, "build_opener", build_opener)

    assert e2e._fetch_json(
        "http://127.0.0.1:8123/private",
        headers={"Authorization": "Bearer test-only"},
    ) == {"status": "ok"}
    handlers = cast(tuple[object, ...], captured["handlers"])
    assert len(handlers) == 1
    assert isinstance(handlers[0], e2e.urllib.request.ProxyHandler)
    assert handlers[0].proxies == {}


def test_validate_root_rejects_repo_ancestor_and_symlink(tmp_path: Path) -> None:
    with pytest.raises(e2e.OperatorError, match="secure leave operation rejected") as ancestor:
        e2e._validate_root(e2e.REPOSITORY_ROOT.parent)
    assert ancestor.value.code is e2e.ExitCode.ROOT_INVALID

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(e2e.OperatorError) as symlink:
        e2e._validate_root(link.resolve().parent / link.name)
    assert symlink.value.code is e2e.ExitCode.ROOT_INVALID


def test_layout_is_idempotent_and_enforces_private_modes(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    assert e2e._ensure_layout(layout.root) == layout
    assert e2e._path_mode(layout.root) == 0o700
    assert all(e2e._path_mode(path) == 0o700 for path in (layout.secrets, layout.pki, layout.artifacts, layout.run, layout.logs, layout.evidence))
    layout.logs.chmod(0o755)
    with pytest.raises(e2e.OperatorError) as error:
        e2e._ensure_layout(layout.root)
    assert error.value.detail == "layout_mode"


@pytest.mark.skipif(not hasattr(e2e.os, "geteuid"), reason="POSIX ownership check")
def test_layout_and_private_files_reject_foreign_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    private_file = layout.run / "owned.json"
    e2e._write_json(private_file, {"ok": True})
    monkeypatch.setattr(e2e.os, "geteuid", lambda: e2e.os.getuid() + 1)

    with pytest.raises(e2e.OperatorError) as root_error:
        e2e._ensure_layout(layout.root)
    assert root_error.value.detail == "root_owner"

    with pytest.raises(e2e.OperatorError) as file_error:
        e2e._read_json(private_file)
    assert file_error.value.detail == "file_owner"


def test_channel_api_env_accepts_only_explicit_four_key_allowlist() -> None:
    key = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()
    provisioning_key = base64.urlsafe_b64encode(b"p" * 32).rstrip(b"=").decode()
    values = {
        e2e.CHANNEL_KEY_ENV: key,
        e2e.CHANNEL_TOKEN_ENV: "t" * 48,
        "MULTIRAG_IDENTITY__PROVISIONING__ACTIVE_KEY_ID": "active",
        "MULTIRAG_IDENTITY__PROVISIONING__HMAC_KEYRING": json.dumps(
            {"active": provisioning_key},
        ),
    }
    rendered = e2e._channel_env_document(values)
    assert provisioning_key.encode() in rendered
    assert len(e2e._channel_key_ring(key)) == 1

    with pytest.raises(e2e.OperatorError):
        e2e._channel_env_document({**values, "UNEXPECTED": "value"})
    with pytest.raises(e2e.OperatorError):
        e2e._channel_env_document(
            {
                e2e.CHANNEL_KEY_ENV: key,
                e2e.CHANNEL_TOKEN_ENV: "t" * 48,
                "MULTIRAG_IDENTITY__PROVISIONING__ACTIVE_KEY_ID": "active",
            },
        )


def test_p3_bootstrap_uses_kid_option_exactly_once(tmp_path: Path) -> None:
    argv = e2e._p3_keygen_argv(tmp_path)
    assert argv.count("--kid") == 1
    assert argv[argv.index("--kid") + 1] == e2e.P3_KEY_ID


def test_bootstrap_reuses_manifest_bytes_and_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ofmcp = tmp_path / "ofmcp"
    ofmcp.mkdir()
    (ofmcp / "AGENTS.md").write_text("test", encoding="utf-8")
    root = (tmp_path / "deployment-root").resolve()
    pki = {
        "ca": {"cert_file": str(root / "pki/ca.pem"), "sha256": "a" * 64},
        "leaves": {},
    }
    secrets_manifest = {"channel": {"active_key_id": "kid"}}
    monkeypatch.setattr(e2e, "_generate_pki", lambda *args, **kwargs: pki)
    monkeypatch.setattr(
        e2e,
        "_bootstrap_dependencies",
        lambda *args, **kwargs: secrets_manifest,
    )

    first = e2e.bootstrap(
        root,
        ofmcp_repo=ofmcp,
        ports=e2e.DEFAULT_PORTS,
    )
    manifest_path = root / "deployment.json"
    original = manifest_path.read_bytes()
    original_mtime = manifest_path.stat().st_mtime_ns
    second = e2e.bootstrap(
        root,
        ofmcp_repo=ofmcp,
        ports=e2e.DEFAULT_PORTS,
    )

    assert manifest_path.read_bytes() == original
    assert manifest_path.stat().st_mtime_ns == original_mtime
    assert first["manifest_sha256"] == second["manifest_sha256"]


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://user@127.0.0.1/a6", "a6"),
        ("postgresql://user@127.0.0.1/a6?dbname=multirag", "multirag"),
    ],
)
def test_effective_postgres_dbname_honors_libpq_override(dsn: str, expected: str) -> None:
    assert e2e._effective_postgres_dbname(dsn) == expected


def test_effective_postgres_dbname_rejects_ambiguous_override() -> None:
    with pytest.raises(e2e.OperatorError) as error:
        e2e._effective_postgres_dbname("postgresql://user@127.0.0.1/a?dbname=b&dbname=c")
    assert error.value.detail == "a6_database_ambiguous"


def test_postgres_dsn_percent_encodes_credentials_and_database() -> None:
    dsn = e2e._postgres_dsn(
        host="127.0.0.1",
        port=5432,
        user="user:name",
        password="p@ss/word",
        database="ofmcp a6",
    )
    assert dsn == ("postgresql://user%3Aname:p%40ss%2Fword@127.0.0.1:5432/ofmcp%20a6")


def test_process_classifier_is_exact_not_substring() -> None:
    assert e2e._classify_process(("/venv/bin/python", "-m", "api.multirag_server")) == "api"
    assert e2e._classify_process(("python", "-m", "api.multirag_server", "--extra")) is None
    assert e2e._classify_process(("echo", "-m", "api.multirag_server")) is None
    assert (
        e2e._classify_process(
            (
                "/venv/bin/ofmcp",
                "serve",
                "--profile",
                "secure",
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
                "--tls-cert-file",
                "/tmp/cert",
                "--tls-key-file",
                "/tmp/key",
            ),
        )
        == "gateway"
    )


def test_api_environment_drops_inherited_identity_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    channel = layout.secrets / "channel"
    channel.mkdir(mode=0o700)
    key = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()
    e2e._atomic_write(
        channel / "api.env",
        e2e._channel_env_document(
            {
                e2e.CHANNEL_KEY_ENV: key,
                e2e.CHANNEL_TOKEN_ENV: "t" * 48,
            },
        ),
    )
    monkeypatch.setenv("MULTIRAG_IDENTITY__MCP_INTERACTIONS__ENABLED", "true")
    monkeypatch.setenv("MULTIRAG_CONFIG_OVERLAY_FILE", "/tmp/attacker.yaml")
    monkeypatch.setenv("MULTIRAG_POSTGRESQL__HOST", "127.0.0.1")

    environment = e2e._api_environment(layout, _manifest(tmp_path), stage="a")

    assert "MULTIRAG_IDENTITY__MCP_INTERACTIONS__ENABLED" not in environment
    assert environment["MULTIRAG_CONFIG_OVERLAY_FILE"] == str(layout.artifacts / "api-stage-a.yaml")
    assert environment["MULTIRAG_POSTGRESQL__HOST"] == "127.0.0.1"
    assert environment["MULTIRAG_MULTIRAG__HTTP_PORT"] == "8123"


def test_policy_environment_drops_inherited_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OFMCP_GATEWAY_AUTH_EXPECTED_AUDIENCE", "https://attacker.invalid")
    monkeypatch.setenv("OFMCP_GATEWAY_AUTH_UNREVIEWED", "bad")
    environment = e2e._policy_environment(_candidate(), _manifest(tmp_path))
    assert environment["OFMCP_GATEWAY_AUTH_EXPECTED_AUDIENCE"] == "https://127.0.0.1:8765/mcp"
    assert "OFMCP_GATEWAY_AUTH_UNREVIEWED" not in environment


def test_full_overlay_freezes_effective_non_identity_sections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from common import app_config, bootstrap

    layout = _layout(tmp_path)
    interaction_key = layout.secrets / "interaction-payload-key.txt"
    e2e._atomic_write(interaction_key, b"interaction-key")
    source = {
        "postgresql": {"host": "db.internal", "dbname": "multirag"},
        "channels": {"control": {"heartbeat_interval_seconds": 15}},
        "identity": {"mcp_interactions": {"enabled": False}},
    }
    original = copy.deepcopy(source)
    config = SimpleNamespace(
        raw=source,
        identity={"mcp_interactions": {"enabled": False}},
    )
    monkeypatch.setattr(app_config, "get_app_config", lambda: config)
    monkeypatch.setattr(bootstrap, "ensure_initialized", lambda **kwargs: None)
    manifest = _manifest(tmp_path)
    manifest["secrets"] = {
        "p3": {
            "kid": "p3",
            "private_key_file": "/private.pem",
            "public_key_file": "/public.pem",
        },
        "interaction": {"key_file": str(interaction_key)},
    }
    manifest["pki"] = {"ca": {"cert_file": "/ca.pem"}}

    overlay = e2e._build_identity_overlay(
        layout,
        manifest,
        interactions_enabled=True,
    )

    assert overlay["postgresql"] == source["postgresql"]
    assert overlay["channels"] == source["channels"]
    assert cast(dict[str, Any], overlay["identity"])["mcp_interactions"] == {
        "enabled": True,
        "payload_encryption_keys": ["interaction-key"],
    }
    assert source == original


def test_grant_generation_is_reused_until_authority_semantics_change(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    path = layout.artifacts / "mcp-grants.json"
    candidate = _candidate()
    initial = e2e._grant_document(
        candidate,
        audience="https://127.0.0.1:8765/mcp",
        policy_revision="a" * 64,
        generation=7,
    )
    e2e._write_json(path, initial)
    e2e._write_json(
        layout.artifacts / "mcp-grants.source.json",
        e2e._grant_source_document(
            candidate,
            audience="https://127.0.0.1:8765/mcp",
            policy_revision="a" * 64,
        ),
    )

    repeated = e2e._grant_for_publish(
        path,
        candidate,
        audience="https://127.0.0.1:8765/mcp",
        policy_revision="a" * 64,
    )
    changed = e2e._grant_for_publish(
        path,
        replace(candidate, binding_generation=2),
        audience="https://127.0.0.1:8765/mcp",
        policy_revision="a" * 64,
    )

    assert repeated == initial
    assert repeated["credential_generation"] == 7
    assert changed["credential_generation"] == 8


def test_operation_lock_rejects_concurrent_owner(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with e2e._operation_lock(layout):
        with pytest.raises(e2e.OperatorError) as error, e2e._operation_lock(layout):
            pass
    assert error.value.detail == "operation_in_progress"


def test_operation_lock_is_global_across_deployment_roots(tmp_path: Path) -> None:
    first = e2e._ensure_layout((tmp_path / "first").resolve())
    second = e2e._ensure_layout((tmp_path / "second").resolve())
    with e2e._operation_lock(first):
        with pytest.raises(e2e.OperatorError) as error, e2e._operation_lock(second):
            pass
    assert error.value.detail == "operation_in_progress"


def _process_record(*, name: str = "api", pid: int = 987654) -> e2e.ProcessRecord:
    return e2e.ProcessRecord(
        name=name,
        pid=pid,
        create_time=1.0,
        cwd=str(e2e.REPOSITORY_ROOT),
        argv_sha256="a" * 64,
        process_group=pid,
        repo_head_sha="b" * 40,
        repo_dirty=False,
        repo_state_sha256="c" * 64,
        api_stage="b" if name == "api" else None,
        overlay_sha256="d" * 64 if name == "api" else None,
    )


def test_process_manager_prunes_only_dead_record_without_orphan_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    manager = e2e.ProcessManager(layout)
    manager._save_records({"api": _process_record()})
    monkeypatch.setattr(e2e.psutil, "pid_exists", lambda _pid: False)
    monkeypatch.setattr(e2e, "_process_group_members", lambda _group: ())

    assert manager.prune_definitely_dead_records() == ("api",)
    assert manager._load_records() == {}


def test_process_manager_rejects_dead_leader_with_orphan_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    manager = e2e.ProcessManager(layout)
    manager._save_records({"supervisor": _process_record(name="supervisor")})
    monkeypatch.setattr(e2e.psutil, "pid_exists", lambda _pid: False)
    monkeypatch.setattr(e2e, "_process_group_members", lambda _group: (object(),))

    with pytest.raises(e2e.OperatorError) as error:
        manager.prune_definitely_dead_records()
    assert error.value.detail.startswith("process=supervisor orphan_group=")


def test_process_manager_start_rechecks_orphan_group_after_pid_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    manager = e2e.ProcessManager(layout)
    manager._save_records({"api": _process_record()})
    monkeypatch.setattr(
        e2e.psutil,
        "Process",
        lambda _pid: (_ for _ in ()).throw(e2e.psutil.NoSuchProcess(_pid)),
    )
    monkeypatch.setattr(e2e, "_process_group_members", lambda _group: (object(),))

    with pytest.raises(e2e.OperatorError) as error:
        manager.start(e2e.ProcessSpec("api", (), e2e.REPOSITORY_ROOT, port=8123))
    assert error.value.detail.startswith("process=api orphan_group=")


def test_listener_uses_scoped_lsof_after_global_inventory_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(pid=45677)
    monkeypatch.setattr(
        e2e.psutil,
        "net_connections",
        lambda *, kind: (_ for _ in ()).throw(e2e.psutil.AccessDenied(47817)),
    )
    monkeypatch.setattr(
        e2e,
        "_lsof_listener_owners",
        lambda port: frozenset({process.pid}) if port == 8123 else frozenset(),
    )
    monkeypatch.setattr(e2e.psutil, "Process", lambda _pid: process)

    assert e2e._listener(8123) is process
    assert e2e._listener(8765) is None


def test_lsof_listener_inventory_uses_private_fixed_command_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(argv: object, **kwargs: object) -> SimpleNamespace:
        captured["argv"] = argv
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=b"45677\n45677\n", stderr=b"")

    monkeypatch.setattr(e2e, "_trusted_lsof_path", lambda: Path("/usr/sbin/lsof"))
    monkeypatch.setattr(e2e.subprocess, "run", run)

    assert e2e._lsof_listener_owners(8123) == frozenset({45677})
    assert captured["argv"] == (
        "/usr/sbin/lsof",
        "-nP",
        "-a",
        "-t",
        "-iTCP:8123",
        "-sTCP:LISTEN",
    )
    assert captured["env"] == {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }
    assert captured["stdin"] is e2e.subprocess.DEVNULL
    assert captured["timeout"] == 5


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (1, b"", b"", frozenset()),
        (0, b"45677\n45678\n", b"", frozenset({45677, 45678})),
    ],
)
def test_lsof_listener_inventory_handles_free_and_multiple_owners(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
    expected: frozenset[int],
) -> None:
    monkeypatch.setattr(e2e, "_trusted_lsof_path", lambda: Path("/usr/sbin/lsof"))
    monkeypatch.setattr(
        e2e.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )

    assert e2e._lsof_listener_owners(8123) == expected


@pytest.mark.parametrize("port", [0, 65_536, True, "8123"])
def test_lsof_listener_inventory_rejects_invalid_port(port: object) -> None:
    with pytest.raises(e2e.OperatorError) as error:
        e2e._lsof_listener_owners(cast(Any, port))
    assert error.value.code is e2e.ExitCode.ARGUMENT_INVALID
    assert error.value.detail == "port"


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (0, b"", b""),
        (1, b"45677\n", b""),
        (2, b"", b""),
        (0, b"not-a-pid\n", b""),
        (0, b"0\n", b""),
        (0, b"45677\n", b"warning"),
        (1, b"", b"warning"),
    ],
)
def test_lsof_listener_inventory_fails_closed_on_ambiguous_output(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
) -> None:
    monkeypatch.setattr(e2e, "_trusted_lsof_path", lambda: Path("/usr/sbin/lsof"))
    monkeypatch.setattr(
        e2e.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )

    with pytest.raises(e2e.OperatorError) as error:
        e2e._lsof_listener_owners(8123)
    assert error.value.code is e2e.ExitCode.PREREQUISITE_MISSING
    assert error.value.detail == "port_inventory"


def test_listener_rejects_multiple_lsof_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        e2e.psutil,
        "net_connections",
        lambda *, kind: (_ for _ in ()).throw(e2e.psutil.AccessDenied()),
    )
    monkeypatch.setattr(
        e2e,
        "_lsof_listener_owners",
        lambda _port: frozenset({45677, 45678}),
    )

    with pytest.raises(e2e.OperatorError) as error:
        e2e._listener(8123)
    assert error.value.code is e2e.ExitCode.PORT_IN_USE
    assert error.value.detail == "port=8123 pid=multiple"


def test_listener_fails_closed_when_inventory_owner_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = SimpleNamespace(
        status=e2e.psutil.CONN_LISTEN,
        laddr=SimpleNamespace(port=8123),
        pid=45677,
    )
    monkeypatch.setattr(
        e2e.psutil,
        "net_connections",
        lambda *, kind: (connection,),
    )
    monkeypatch.setattr(
        e2e.psutil,
        "Process",
        lambda pid: (_ for _ in ()).throw(e2e.psutil.NoSuchProcess(pid)),
    )

    with pytest.raises(e2e.OperatorError) as error:
        e2e._listener(8123)
    assert error.value.code is e2e.ExitCode.PREREQUISITE_MISSING
    assert error.value.detail == "port_inventory"


def test_stop_without_record_preserves_target_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = e2e.ProcessManager(_layout(tmp_path))
    observed: list[e2e.ProcessSpec] = []
    monkeypatch.setattr(manager, "_take_over_matching", observed.append)
    spec = e2e.ProcessSpec("gateway", (), tmp_path, port=8765)

    manager.stop(spec)

    assert observed == [spec]


def test_verify_record_rejects_process_group_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = e2e.ProcessManager(_layout(tmp_path))
    record = replace(
        _process_record(),
        cwd=str(tmp_path.resolve()),
        argv_sha256=e2e._argv_hash(("python", "-m", "api.multirag_server")),
    )
    process = SimpleNamespace(
        pid=record.pid,
        create_time=lambda: record.create_time,
    )
    monkeypatch.setattr(e2e.psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(e2e, "_process_cwd", lambda _process: tmp_path.resolve())
    monkeypatch.setattr(
        e2e,
        "_process_cmdline",
        lambda _process: ("python", "-m", "api.multirag_server"),
    )
    monkeypatch.setattr(e2e.os, "getpgid", lambda _pid: record.process_group + 1)

    with pytest.raises(e2e.OperatorError) as error:
        manager._verify_record(
            record,
            e2e.ProcessSpec("api", (), tmp_path.resolve(), port=8123),
        )
    assert error.value.detail.endswith("provenance_mismatch")


def test_spawn_cleanup_kills_confirmed_orphan_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(
        pid=45678,
        wait=lambda timeout: (_ for _ in ()).throw(ProcessLookupError()),
        terminate=lambda: None,
        kill=lambda: None,
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        e2e.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(e2e, "_process_group_members", lambda _group: (object(),))
    monkeypatch.setattr(
        e2e.os,
        "killpg",
        lambda group, sent_signal: signals.append((group, sent_signal)),
    )
    monkeypatch.setattr(
        e2e,
        "_wait_for_group_exit",
        lambda _group, *, seconds: seconds >= 5,
    )

    e2e._terminate_spawned_process(cast(Any, process))

    assert signals == [
        (process.pid, e2e.signal.SIGTERM),
        (process.pid, e2e.signal.SIGKILL),
    ]


def test_stop_confirmed_process_drains_group_when_leader_exits_during_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(
        pid=45679,
        terminate=lambda: (_ for _ in ()).throw(e2e.psutil.NoSuchProcess(45679)),
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        e2e.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(e2e, "_process_group_members", lambda _group: (object(),))
    monkeypatch.setattr(
        e2e.os,
        "killpg",
        lambda group, sent_signal: signals.append((group, sent_signal)),
    )
    monkeypatch.setattr(
        e2e,
        "_wait_for_group_exit",
        lambda _group, *, seconds: seconds >= 3,
    )

    e2e._stop_confirmed_process(cast(Any, process), process_group=process.pid)

    assert signals == [(process.pid, e2e.signal.SIGTERM)]


def test_runtime_argv_matching_requires_exact_target_values(tmp_path: Path) -> None:
    spec = e2e.ProcessSpec(
        "gateway",
        (
            "uv",
            "run",
            "ofmcp",
            "serve",
            "--profile",
            "secure",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--tls-cert-file",
            "/target/cert",
            "--tls-key-file",
            "/target/key",
        ),
        tmp_path,
        port=8765,
    )
    exact = ("/venv/bin/ofmcp", *spec.argv[3:])
    wrong_cert = tuple("/other/cert" if item == "/target/cert" else item for item in exact)

    assert e2e._runtime_argv_matches_spec(exact, spec)
    assert not e2e._runtime_argv_matches_spec(wrong_cert, spec)


def test_preflight_counts_exact_process_before_port_bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    process = SimpleNamespace(pid=45680)
    monkeypatch.setattr(
        e2e.ProcessManager,
        "snapshot",
        lambda self, _specs: {name: {"state": "stopped"} for name in e2e.PROCESS_ORDER},
    )
    monkeypatch.setattr(
        e2e,
        "_matching_processes",
        lambda spec: (process,) if spec.name == "api" else (),
    )
    monkeypatch.setattr(e2e, "_listener", lambda _port: None)
    monkeypatch.setattr(e2e.psutil, "process_iter", lambda: ())

    observed = e2e._preflight_inventory(layout, _manifest(tmp_path))

    assert observed["api"] == {
        "port": e2e.DEFAULT_PORTS["api"],
        "state": "same_repo",
        "pid": process.pid,
        "phase": "starting",
    }


def test_takeover_stops_exact_starting_process_before_checking_listener(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = e2e.ProcessManager(_layout(tmp_path))
    process = SimpleNamespace(pid=45681)
    actions: list[str] = []
    monkeypatch.setattr(e2e, "_matching_processes", lambda _spec: (process,))
    monkeypatch.setattr(e2e.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        e2e,
        "_stop_confirmed_process",
        lambda target, *, process_group: actions.append(
            f"stop:{target.pid}:{process_group}",
        ),
    )
    monkeypatch.setattr(
        e2e,
        "_listener",
        lambda _port: actions.append("listener") or None,
    )

    manager._take_over_matching(
        e2e.ProcessSpec("api", (), e2e.REPOSITORY_ROOT, port=8123),
    )

    assert actions == [f"stop:{process.pid}:{process.pid}", "listener"]


class _FakeManager:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def start(self, spec: e2e.ProcessSpec) -> e2e.ProcessRecord:
        self.actions.append(f"start:{spec.name}")
        return Any  # type: ignore[return-value]

    def stop(self, spec: e2e.ProcessSpec) -> None:
        self.actions.append(f"stop:{spec.name}")


def test_stage_b_rollback_disables_producer_before_consumer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    specs = {name: e2e.ProcessSpec(name, (), tmp_path) for name in ("api", "supervisor", "gateway", "mock", "jwks")}
    manager = _FakeManager()
    monkeypatch.setattr(e2e, "_build_specs", lambda *args, **kwargs: specs)
    monkeypatch.setattr(e2e, "_wait_for", lambda *args, **kwargs: None)
    e2e._rollback_failed_up(
        manager,  # type: ignore[arg-type]
        layout,
        _manifest(tmp_path),
        _candidate(),
        started=set(specs),
    )
    assert manager.actions == [
        "stop:api",
        "start:api",
        "stop:supervisor",
        "stop:gateway",
        "stop:mock",
        "stop:jwks",
        "stop:api",
    ]


def test_pre_stage_b_rollback_uses_stage_neutral_api_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    actions: list[str] = []

    def specs(
        *_args: object,
        api_stage: str,
        **_kwargs: object,
    ) -> dict[str, e2e.ProcessSpec]:
        return {
            name: e2e.ProcessSpec(
                name,
                (),
                tmp_path,
                env={"stage": api_stage} if name == "api" else {},
            )
            for name in ("api", "supervisor", "gateway", "mock", "jwks")
        }

    inventory = {name: e2e.ProcessSpec(name, (), tmp_path) for name in ("api", "supervisor", "gateway", "mock", "jwks")}

    class StageABoundManager:
        def start(self, spec: e2e.ProcessSpec) -> e2e.ProcessRecord:
            actions.append(f"start:{spec.name}:{spec.env.get('stage', 'neutral')}")
            return _process_record(name=spec.name, pid=54000 + len(actions))

        def stop(self, spec: e2e.ProcessSpec) -> None:
            stage = spec.env.get("stage", "neutral")
            actions.append(f"stop:{spec.name}:{stage}")
            if spec.name == "api" and stage != "neutral":
                raise e2e.OperatorError(e2e.ExitCode.PROCESS_NOT_OWNED)

    monkeypatch.setattr(e2e, "_build_specs", specs)
    monkeypatch.setattr(e2e, "_inventory_specs", lambda _manifest: inventory)

    e2e._rollback_failed_up(
        StageABoundManager(),  # type: ignore[arg-type]
        layout,
        _manifest(tmp_path),
        _candidate(),
        started={"api", "gateway", "mock", "jwks"},
    )

    assert actions == [
        "stop:api:neutral",
        "stop:gateway:neutral",
        "stop:mock:neutral",
        "stop:jwks:neutral",
    ]


def test_up_interrupt_after_stage_b_start_runs_fail_closed_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["pki"] = {"ca": {"cert_file": str(tmp_path / "ca.pem")}}
    specs = {name: e2e.ProcessSpec(name, (), tmp_path) for name in ("api", "supervisor", "gateway", "mock", "jwks")}

    class Manager(_FakeManager):
        def prune_definitely_dead_records(self) -> None:
            return None

        def start(self, spec: e2e.ProcessSpec) -> e2e.ProcessRecord:
            self.actions.append(f"start:{spec.name}")
            return _process_record(
                name=spec.name,
                pid=53000 + len(self.actions),
            )

    manager = Manager()
    waits = 0
    rollback: list[set[str]] = []

    def wait_for(*_args: object, **_kwargs: object) -> None:
        nonlocal waits
        waits += 1
        if waits == 5:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        e2e,
        "_load_deployment",
        lambda _root: (layout, manifest),
    )
    monkeypatch.setattr(e2e, "ProcessManager", lambda _layout: manager)
    monkeypatch.setattr(e2e, "_preflight_inventory", lambda *_args: {})
    monkeypatch.setattr(e2e, "_quiesce_managed_runtime", lambda *_args: None)
    monkeypatch.setattr(e2e, "_migrate", lambda *_args: None)
    monkeypatch.setattr(e2e, "prepare", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(e2e, "_selected_candidate", lambda _layout: _candidate())
    monkeypatch.setattr(e2e, "_build_specs", lambda *_args, **_kwargs: specs)
    monkeypatch.setattr(e2e, "_record_stage", lambda *_args: None)
    monkeypatch.setattr(e2e, "_wait_for", wait_for)
    monkeypatch.setattr(
        e2e,
        "_validate_gateway_before_consumer",
        lambda *_args: None,
    )
    monkeypatch.setattr(e2e, "_wait_for_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        e2e,
        "_validate_interaction_capability",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        e2e,
        "_rollback_failed_up",
        lambda *_args, started, **_kwargs: rollback.append(set(started)),
    )

    with pytest.raises(KeyboardInterrupt):
        e2e._up_unlocked(layout.root)

    assert rollback == [{"jwks", "api", "mock", "gateway", "supervisor"}]


def test_managed_record_check_uses_exact_stage_b_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    spec = e2e.ProcessSpec("api", (), tmp_path, env={"stage": "b"})
    record = object()
    observed: list[tuple[object, e2e.ProcessSpec]] = []
    manager = SimpleNamespace(
        _load_records=lambda: {"api": record},
        _verify_record=lambda actual, expected: observed.append((actual, expected)),
    )
    monkeypatch.setattr(e2e, "ProcessManager", lambda _layout: manager)

    e2e._verify_managed_record(layout, spec)

    assert observed == [(record, spec)]


def test_quiesce_reopens_stage_a_before_draining_supervisor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    specs = {name: e2e.ProcessSpec(name, (), tmp_path) for name in ("api", "supervisor", "gateway", "mock", "jwks")}
    manager = _FakeManager()
    monkeypatch.setattr(
        e2e,
        "_preflight_inventory",
        lambda *_args: {"supervisor": {"state": "same_repo"}},
    )
    monkeypatch.setattr(e2e, "ProcessManager", lambda _layout: manager)
    monkeypatch.setattr(e2e, "_inventory_specs", lambda _manifest: specs)
    monkeypatch.setattr(e2e, "_build_specs", lambda *args, **kwargs: specs)
    monkeypatch.setattr(e2e, "_selected_candidate", lambda _layout: _candidate())
    monkeypatch.setattr(e2e, "_wait_for", lambda *args, **kwargs: None)

    e2e._quiesce_managed_runtime(layout, _manifest(tmp_path))

    assert manager.actions == [
        "stop:api",
        "start:api",
        "stop:supervisor",
        "stop:gateway",
        "stop:mock",
        "stop:jwks",
        "stop:api",
    ]


def test_prepare_quiescence_counts_recorded_process_before_listener_bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    manager = SimpleNamespace(
        snapshot=lambda _specs: {
            "api": {"state": "running"},
            "gateway": {"state": "stopped"},
            "jwks": {"state": "stopped"},
            "mock": {"state": "stopped"},
            "supervisor": {"state": "stopped"},
        },
    )
    monkeypatch.setattr(e2e, "_preflight_inventory", lambda *_args: {})
    monkeypatch.setattr(e2e, "ProcessManager", lambda _layout: manager)

    with pytest.raises(e2e.OperatorError) as error:
        e2e._require_quiescent_runtime(layout, _manifest(tmp_path))

    assert error.value.detail == "runtime_active=api"


def test_down_disables_api_before_stage_a_configuration_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    manager = _FakeManager()
    monkeypatch.setattr(
        e2e,
        "_load_deployment",
        lambda _root: (layout, _manifest(tmp_path)),
    )
    monkeypatch.setattr(e2e, "ProcessManager", lambda _layout: manager)
    monkeypatch.setattr(
        e2e,
        "_selected_candidate",
        lambda _layout: (_ for _ in ()).throw(
            e2e.OperatorError(e2e.ExitCode.ARTIFACT_INVALID, "selection"),
        ),
    )

    with pytest.raises(e2e.OperatorError) as error:
        e2e._down_unlocked(layout.root)

    assert error.value.detail == "down=stage_a_config"
    assert manager.actions == ["stop:api"]


def test_rollback_keeps_dependencies_when_stage_a_readiness_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    specs = {name: e2e.ProcessSpec(name, (), tmp_path) for name in ("api", "supervisor", "gateway", "mock", "jwks")}
    manager = _FakeManager()
    monkeypatch.setattr(e2e, "_build_specs", lambda *args, **kwargs: specs)

    def fail_readiness(*args: object, **kwargs: object) -> None:
        raise e2e.OperatorError(e2e.ExitCode.PROCESS_FAILED, "readiness")

    monkeypatch.setattr(e2e, "_wait_for", fail_readiness)
    with pytest.raises(e2e.OperatorError, match="secure leave operation rejected"):
        e2e._rollback_failed_up(
            manager,  # type: ignore[arg-type]
            layout,
            _manifest(tmp_path),
            _candidate(),
            started=set(specs),
        )

    assert manager.actions == ["stop:api", "start:api"]


def test_rollback_supervisor_failure_is_hard_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    specs = {name: e2e.ProcessSpec(name, (), tmp_path) for name in ("api", "supervisor", "gateway", "mock", "jwks")}

    class FailingSupervisorManager(_FakeManager):
        def stop(self, spec: e2e.ProcessSpec) -> None:
            self.actions.append(f"stop:{spec.name}")
            if spec.name == "supervisor":
                raise e2e.OperatorError(e2e.ExitCode.PROCESS_NOT_OWNED)

    manager = FailingSupervisorManager()
    monkeypatch.setattr(e2e, "_build_specs", lambda *args, **kwargs: specs)
    monkeypatch.setattr(e2e, "_wait_for", lambda *args, **kwargs: None)

    with pytest.raises(e2e.OperatorError) as error:
        e2e._rollback_failed_up(
            manager,  # type: ignore[arg-type]
            layout,
            _manifest(tmp_path),
            _candidate(),
            started=set(specs),
        )

    assert error.value.detail == "rollback=supervisor"
    assert manager.actions == ["stop:api", "start:api", "stop:supervisor"]


def test_waiting_interaction_restart_touches_only_api_and_supervisor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    actions: list[str] = []

    class RestartManager:
        def start(self, spec: e2e.ProcessSpec) -> e2e.ProcessRecord:
            stage = spec.env.get("stage", "shared")
            actions.append(f"start:{spec.name}:{stage}")
            return _process_record(name=spec.name, pid=50000 + len(actions))

        def stop(self, spec: e2e.ProcessSpec) -> None:
            stage = spec.env.get("stage", "shared")
            actions.append(f"stop:{spec.name}:{stage}")

    def specs(*_args: object, api_stage: str, **_kwargs: object) -> dict[str, e2e.ProcessSpec]:
        return {
            name: e2e.ProcessSpec(
                name,
                (),
                tmp_path,
                env={"stage": api_stage if name == "api" else "shared"},
            )
            for name in e2e.PROCESS_ORDER
        }

    monkeypatch.setattr(e2e, "ProcessManager", lambda _layout: RestartManager())
    monkeypatch.setattr(e2e, "_build_specs", specs)
    monkeypatch.setattr(e2e, "_wait_for", lambda *args, **kwargs: None)
    monkeypatch.setattr(e2e, "_wait_for_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        e2e,
        "_validate_interaction_capability",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        e2e,
        "doctor",
        lambda *args, **kwargs: {"status": "ready"},
    )
    runtime_before = {name: {"pid": index} for index, name in enumerate(e2e.PROCESS_ORDER)}
    runtime_after = copy.deepcopy(runtime_before)
    runtime_after["api"] = {"pid": 101}
    runtime_after["supervisor"] = {"pid": 104}
    runtime_values = iter((runtime_before, runtime_after))
    monkeypatch.setattr(
        e2e,
        "_runtime_repository_evidence",
        lambda *args, **kwargs: next(runtime_values),
    )
    monkeypatch.setattr(
        e2e,
        "_mock_run_boundary",
        lambda *args, **kwargs: {"pid": 2, "calls": {}},
    )

    result = e2e._restart_waiting_runtime(
        layout.root,
        layout,
        _manifest(tmp_path),
        _candidate(),
    )

    assert result.response["status"] == "awaiting_feishu"
    assert actions == [
        "stop:api:b",
        "start:api:a",
        "stop:supervisor:shared",
        "start:supervisor:shared",
        "stop:api:a",
        "start:api:b",
    ]


def test_waiting_interaction_restart_keeps_stage_a_on_supervisor_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    actions: list[str] = []

    class FailingRestartManager:
        def start(self, spec: e2e.ProcessSpec) -> e2e.ProcessRecord:
            actions.append(f"start:{spec.name}")
            return _process_record(name=spec.name, pid=50000 + len(actions))

        def stop(self, spec: e2e.ProcessSpec) -> None:
            actions.append(f"stop:{spec.name}")
            if spec.name == "supervisor":
                raise e2e.OperatorError(e2e.ExitCode.PROCESS_NOT_OWNED)

    specs = {name: e2e.ProcessSpec(name, (), tmp_path) for name in e2e.PROCESS_ORDER}
    monkeypatch.setattr(
        e2e,
        "ProcessManager",
        lambda _layout: FailingRestartManager(),
    )
    monkeypatch.setattr(e2e, "_build_specs", lambda *args, **kwargs: specs)
    monkeypatch.setattr(e2e, "_wait_for", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        e2e,
        "_runtime_repository_evidence",
        lambda *args, **kwargs: {name: {"pid": index} for index, name in enumerate(e2e.PROCESS_ORDER)},
    )
    monkeypatch.setattr(
        e2e,
        "_mock_run_boundary",
        lambda *args, **kwargs: {"pid": 2, "calls": {}},
    )

    with pytest.raises(e2e.OperatorError) as error:
        e2e._restart_waiting_runtime(
            layout.root,
            layout,
            _manifest(tmp_path),
            _candidate(),
        )

    assert error.value.detail == "restart_waiting=supervisor"
    assert actions == ["stop:api", "start:api", "stop:supervisor"]


def test_waiting_restart_recovers_stage_a_immediately_after_raw_stage_b_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    actions: list[str] = []

    class RestartManager:
        def start(self, spec: e2e.ProcessSpec) -> e2e.ProcessRecord:
            actions.append(f"start:{spec.name}:{spec.env.get('stage', 'inventory')}")
            return _process_record(name=spec.name, pid=52000 + len(actions))

        def stop(self, spec: e2e.ProcessSpec) -> None:
            actions.append(f"stop:{spec.name}:{spec.env.get('stage', 'inventory')}")

    def specs(*_args: object, api_stage: str, **_kwargs: object) -> dict[str, e2e.ProcessSpec]:
        return {
            name: e2e.ProcessSpec(
                name,
                (),
                tmp_path,
                env={"stage": api_stage if name == "api" else "shared"},
            )
            for name in e2e.PROCESS_ORDER
        }

    waits = 0

    def wait_for(*_args: object, **_kwargs: object) -> None:
        nonlocal waits
        waits += 1
        if waits == 2:
            raise OSError("untrusted detail")

    runtime = {name: {"pid": index} for index, name in enumerate(e2e.PROCESS_ORDER)}
    monkeypatch.setattr(e2e, "ProcessManager", lambda _layout: RestartManager())
    monkeypatch.setattr(e2e, "_build_specs", specs)
    monkeypatch.setattr(
        e2e,
        "_inventory_specs",
        lambda _manifest: {name: e2e.ProcessSpec(name, (), tmp_path) for name in e2e.PROCESS_ORDER},
    )
    monkeypatch.setattr(e2e, "_wait_for", wait_for)
    monkeypatch.setattr(e2e, "_wait_for_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        e2e,
        "_validate_interaction_capability",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(e2e, "doctor", lambda *args, **kwargs: {"status": "ready"})
    monkeypatch.setattr(
        e2e,
        "_runtime_repository_evidence",
        lambda *args, **kwargs: runtime,
    )
    monkeypatch.setattr(
        e2e,
        "_mock_run_boundary",
        lambda *args, **kwargs: {"pid": 2, "calls": {}},
    )

    with pytest.raises(OSError):
        e2e._restart_waiting_runtime(
            layout.root,
            layout,
            _manifest(tmp_path),
            _candidate(),
        )

    assert actions == [
        "stop:api:b",
        "start:api:a",
        "stop:supervisor:shared",
        "start:supervisor:shared",
        "stop:api:a",
        "start:api:b",
        "stop:api:inventory",
        "start:api:a",
    ]


def test_failed_waiting_restart_reconciliation_leaves_stage_a_with_fresh_consumer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    actions: list[str] = []

    class RecoveryManager:
        def start(self, spec: e2e.ProcessSpec) -> e2e.ProcessRecord:
            actions.append(f"start:{spec.name}:{spec.env.get('stage', 'shared')}")
            return _process_record(name=spec.name, pid=51000 + len(actions))

        def stop(self, spec: e2e.ProcessSpec) -> None:
            actions.append(f"stop:{spec.name}:{spec.env.get('stage', 'inventory')}")

    def specs(*_args: object, api_stage: str, **_kwargs: object) -> dict[str, e2e.ProcessSpec]:
        return {
            name: e2e.ProcessSpec(
                name,
                (),
                tmp_path,
                env={"stage": api_stage if name == "api" else "shared"},
            )
            for name in e2e.PROCESS_ORDER
        }

    inventory = {name: e2e.ProcessSpec(name, (), tmp_path) for name in e2e.PROCESS_ORDER}
    monkeypatch.setattr(e2e, "ProcessManager", lambda _layout: RecoveryManager())
    monkeypatch.setattr(e2e, "_build_specs", specs)
    monkeypatch.setattr(e2e, "_inventory_specs", lambda _manifest: inventory)
    monkeypatch.setattr(e2e, "_wait_for", lambda *args, **kwargs: None)
    monkeypatch.setattr(e2e, "_wait_for_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        e2e,
        "_validate_interaction_capability",
        lambda *args, **kwargs: None,
    )

    e2e._reconcile_failed_waiting_restart(
        layout,
        _manifest(tmp_path),
        _candidate(),
    )

    assert actions == [
        "stop:api:inventory",
        "start:api:a",
        "stop:supervisor:inventory",
        "start:supervisor:shared",
    ]


def test_probe_recovers_failed_restart_before_online_doctor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    candidate = _candidate()
    state = {
        "schema": f"{e2e.SCHEMA}/live-probe",
        "version": e2e.VERSION,
        "status": "awaiting_feishu",
        "started_at": "2026-08-30T01:00:00+00:00",
        "candidate_ref": candidate.ref,
        "waiting_restart": {
            "status": "failed",
            "started_at": "2026-08-30T01:00:01+00:00",
            "failed_stage": "unexpected",
        },
    }
    e2e._write_json(layout.run / "live-probe.json", state)
    actions: list[str] = []
    monkeypatch.setattr(
        e2e,
        "_load_deployment",
        lambda _root: (layout, _manifest(tmp_path)),
    )
    monkeypatch.setattr(e2e, "_selected_candidate", lambda _layout: candidate)
    monkeypatch.setattr(
        e2e,
        "_reconcile_failed_waiting_restart",
        lambda *args, **kwargs: actions.append("reconciled"),
    )
    monkeypatch.setattr(
        e2e,
        "_archive_abandoned_live_probe",
        lambda *args, **kwargs: actions.append("archived"),
    )
    monkeypatch.setattr(
        e2e,
        "doctor",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("doctor ran")),
    )

    with pytest.raises(e2e.OperatorError) as error:
        e2e._probe_unlocked(layout.root, case="leave-preview")

    assert error.value.code is e2e.ExitCode.LIVE_EVIDENCE_PENDING
    assert error.value.detail == "waiting_restart_recovered_run_up"
    assert actions == ["reconciled", "archived"]
    assert not (layout.run / "live-probe.json").exists()


def test_restart_transaction_records_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    candidate = _candidate()
    mock_boundary = {
        "pid": 123,
        "create_time": 1.0,
        "do_create_request_blocked": 0,
        "calls": {},
    }
    static_boundary: dict[str, object] = {"artifact_sha256": {}}
    state = {
        "schema": f"{e2e.SCHEMA}/live-probe",
        "version": e2e.VERSION,
        "status": "awaiting_feishu",
        "started_at": "2026-08-30T01:00:00+00:00",
        "candidate_ref": candidate.ref,
        "mock_boundary": mock_boundary,
        "static_boundary": static_boundary,
    }
    probe_path = layout.run / "live-probe.json"
    e2e._write_json(probe_path, state)

    async def waiting_ok(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        e2e,
        "_load_deployment",
        lambda _root: (layout, _manifest(tmp_path)),
    )
    monkeypatch.setattr(e2e, "_selected_candidate", lambda _layout: candidate)
    monkeypatch.setattr(e2e, "doctor", lambda *args, **kwargs: {"status": "ready"})
    monkeypatch.setattr(e2e, "_mock_run_boundary", lambda *args, **kwargs: mock_boundary)
    monkeypatch.setattr(e2e, "_probe_static_boundary", lambda *args, **kwargs: static_boundary)
    monkeypatch.setattr(e2e, "_validate_waiting_interaction", waiting_ok)
    monkeypatch.setattr(
        e2e,
        "_restart_waiting_runtime",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("untrusted detail")),
    )

    with pytest.raises(OSError):
        e2e._probe_unlocked(
            layout.root,
            case="leave-preview",
            restart_waiting_runtime=True,
        )

    recorded = e2e._read_json(probe_path)["waiting_restart"]
    assert recorded == {
        "status": "failed",
        "started_at": recorded["started_at"],
        "failed_stage": "unexpected",
    }


def test_live_delivery_attempts_count_form_and_terminal_on_same_row() -> None:
    assert e2e._valid_live_delivery_attempts(
        terminal_delivery_attempt=2,
        resume_attempt=1,
        callback_attempt=1,
    )


@pytest.mark.parametrize("wrapped", [False, True])
def test_encrypted_terminal_evidence_requires_strict_v2_source(
    tmp_path: Path,
    wrapped: bool,
) -> None:
    from api.identity.mcp_interactions.crypto import InteractionPayloadCipher

    layout = _layout(tmp_path)
    key = b"k" * 32
    encoded_key = base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")
    (layout.secrets / "interaction-payload-key.txt").write_text(
        encoded_key + "\n",
        encoding="ascii",
    )
    (layout.secrets / "interaction-payload-key.txt").chmod(0o600)
    payload = {
        "kind": "com.ofmcp/interaction-terminal",
        "version": 2,
        "title": "请假试算完成",
        "message": "仅预览，未创建请假单。",
        "fields": [{"label": "请假类型", "value": "事假"}],
        "preview_only": True,
    }
    value: object = {"result": payload} if wrapped else payload
    encrypted = InteractionPayloadCipher([key]).encrypt(
        tenant_id="tenant",
        interaction_id="interaction",
        revision=2,
        purpose="result",
        value=value,
    )
    interaction = SimpleNamespace(
        tenant_id="tenant",
        id="interaction",
        revision=2,
        result_ciphertext=encrypted.ciphertext,
        result_key_id=encrypted.key_id,
    )

    e2e._validate_encrypted_terminal_v2(
        layout,
        interaction,
        {
            "state": "completed",
            "title": payload["title"],
            "message": payload["message"],
            "fields": payload["fields"],
        },
    )


def test_encrypted_terminal_evidence_rejects_v1_source(
    tmp_path: Path,
) -> None:
    from api.identity.mcp_interactions.crypto import InteractionPayloadCipher

    layout = _layout(tmp_path)
    key = b"k" * 32
    encoded_key = base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")
    (layout.secrets / "interaction-payload-key.txt").write_text(
        encoded_key + "\n",
        encoding="ascii",
    )
    encrypted = InteractionPayloadCipher([key]).encrypt(
        tenant_id="tenant",
        interaction_id="interaction",
        revision=2,
        purpose="result",
        value={
            "kind": "com.ofmcp/interaction-terminal",
            "version": 1,
            "message": "历史结果",
            "preview_only": True,
        },
    )
    interaction = SimpleNamespace(
        tenant_id="tenant",
        id="interaction",
        revision=2,
        result_ciphertext=encrypted.ciphertext,
        result_key_id=encrypted.key_id,
    )

    with pytest.raises(e2e.OperatorError) as error:
        e2e._validate_encrypted_terminal_v2(
            layout,
            interaction,
            {
                "state": "completed",
                "title": "请假试算完成",
                "message": "历史结果",
                "fields": [{"label": "请假类型", "value": "事假"}],
            },
        )

    assert error.value.detail == "terminal_result_v2"


def test_runtime_segment_ledger_accepts_only_controlled_api_restart() -> None:
    base_record = {
        "pid": 100,
        "create_time": 1.0,
        "process_group": 100,
        "cwd": str(e2e.REPOSITORY_ROOT),
        "argv_sha256": "a" * 64,
        "repo_head_sha": "b" * 40,
        "repo_dirty": False,
        "repo_state_sha256": "c" * 64,
    }
    initial = {name: dict(base_record) for name in e2e.PROCESS_ORDER}
    completed = copy.deepcopy(initial)
    completed["api"].update(pid=101, process_group=101, create_time=2.0)
    events = [
        {
            "event": "stopped",
            "at": "2026-08-30T01:00:00+00:00",
            "record": {"name": "api", **initial["api"]},
        },
        {
            "event": "started",
            "at": "2026-08-30T01:00:01+00:00",
            "record": {"name": "api", **completed["api"]},
        },
    ]

    segments = e2e._validated_runtime_segments(
        initial=initial,
        completed=completed,
        events=events,
    )
    assert [segment["event"] for segment in segments] == ["stopped", "started"]

    events[0]["record"] = {"name": "gateway", **initial["gateway"]}
    with pytest.raises(e2e.OperatorError) as error:
        e2e._validated_runtime_segments(
            initial=initial,
            completed=completed,
            events=events,
        )
    assert error.value.detail == "runtime_nonrestartable_changed"
    assert not e2e._valid_live_delivery_attempts(
        terminal_delivery_attempt=1,
        resume_attempt=1,
        callback_attempt=1,
    )


def test_evidence_runtime_record_requires_owned_session_group() -> None:
    record = {
        "pid": 100,
        "create_time": 1.0,
        "process_group": 101,
        "cwd": str(e2e.REPOSITORY_ROOT),
        "argv_sha256": "a" * 64,
        "repo_head_sha": "b" * 40,
        "repo_dirty": False,
        "repo_state_sha256": "c" * 64,
    }

    assert not e2e._valid_runtime_record(record, process_name="api")


def test_generate_pki_is_idempotent_and_strict(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    first = e2e._generate_pki(layout, e2e.SubprocessCommandRunner())
    second = e2e._generate_pki(layout, e2e.SubprocessCommandRunner())
    assert first == second
    assert cast(dict[str, object], first["ca"])["sha256"]
    assert set(cast(dict[str, object], first["leaves"])) == {"issuer", "gateway"}


def test_parser_exposes_complete_operator_surface() -> None:
    parser = e2e._build_parser()
    commands = next(action for action in parser._actions if action.dest == "command")
    assert set(cast(dict[str, object], commands.choices)) == {
        "bootstrap",
        "discover",
        "prepare",
        "doctor",
        "up",
        "status",
        "probe",
        "evidence",
        "down",
    }
    probe = cast(Any, commands.choices["probe"])
    assert any(action.dest == "restart_waiting_runtime" for action in probe._actions)


def test_ofmcp_tagged_key_fingerprints_are_strictly_normalized_for_evidence(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    digest = "d" * 64
    ofmcp_manifest = layout.root / "ofmcp-deployment.json"
    e2e._write_json(
        ofmcp_manifest,
        {
            "schema": "com.ofmcp/ops-deployment",
            "version": 1,
            "secrets": {
                "request_state_key_ring": {"fingerprints": [f"sha256:{digest}"]},
                "a6_fingerprint_key_ring": {"fingerprints": [f"sha256:{digest}"]},
                "a6_identity_key": {"fingerprint": f"sha256:{digest}"},
            },
        },
    )
    manifest = {
        "secrets": {
            "channel": {
                "active_key_id": "channel-key",
                "key_ids": ["channel-key"],
                "key_fingerprints": [digest],
            },
            "interaction": {"key_fingerprint": digest},
            "p3": {"kid": "p3-key", "public_key_fingerprint": digest},
            "ecology_mock": {"public_key_fingerprint": digest},
            "ofmcp_manifest": str(ofmcp_manifest),
        },
    }

    evidence = e2e._key_fingerprint_evidence(layout, manifest)

    assert evidence["ofmcp_request_state"] == {"fingerprints": [digest]}
    assert evidence["ofmcp_a6_fingerprint"] == {"fingerprints": [digest]}
    assert evidence["ofmcp_a6_identity"] == {"fingerprint": digest}
    assert e2e._valid_key_fingerprint_evidence(evidence)


@pytest.mark.parametrize(
    "value",
    ["d" * 64, "SHA256:" + "d" * 64, "sha256:" + "D" * 64, "sha256:short"],
)
def test_ofmcp_key_fingerprint_rejects_noncanonical_tag(value: str) -> None:
    with pytest.raises(e2e.OperatorError) as error:
        e2e._require_tagged_sha256(value, detail="fingerprint")
    assert error.value.code is e2e.ExitCode.ARTIFACT_INVALID
    assert error.value.detail == "fingerprint"


def _write_evidence_fixture(path: Path) -> None:
    path.mkdir(mode=0o700)
    digest = "d" * 64
    stage_a_digest = "1" * 64
    stage_b_digest = "2" * 64
    first_trace = "c" * 32
    terminal_trace = "e" * 32
    started_at = "2026-08-30T01:00:00+00:00"
    completed_at = "2026-08-30T01:01:00+00:00"
    repository = {
        "dirty": True,
        "head_sha": "a" * 40,
        "worktree_sha256": "b" * 64,
    }
    runtime = {
        name: {
            "pid": index + 100,
            "create_time": 1.0,
            "process_group": index + 100,
            "cwd": str(e2e.REPOSITORY_ROOT if name in {"jwks", "api", "supervisor"} else e2e.REPOSITORY_ROOT.parent),
            "argv_sha256": digest,
            "repo_head_sha": repository["head_sha"],
            "repo_dirty": repository["dirty"],
            "repo_state_sha256": repository["worktree_sha256"],
            "api_stage": "b" if name == "api" else None,
            "overlay_sha256": stage_b_digest if name == "api" else None,
        }
        for index, name in enumerate(e2e.PROCESS_ORDER)
    }
    runtime_started = copy.deepcopy(runtime)
    runtime_started["api"].update(
        pid=201,
        process_group=201,
        create_time=0.5,
    )
    runtime_started["supervisor"].update(
        pid=204,
        process_group=204,
        create_time=0.5,
    )
    stage_a_api = copy.deepcopy(runtime["api"])
    stage_a_api.update(
        pid=301,
        process_group=301,
        create_time=0.75,
        api_stage="a",
        overlay_sha256=stage_a_digest,
    )
    restart_segments = [
        {
            "event": "stopped",
            "at": "2026-08-30T01:00:10+00:00",
            "process": "api",
            "record": runtime_started["api"],
        },
        {
            "event": "started",
            "at": "2026-08-30T01:00:11+00:00",
            "process": "api",
            "record": stage_a_api,
        },
        {
            "event": "stopped",
            "at": "2026-08-30T01:00:12+00:00",
            "process": "supervisor",
            "record": runtime_started["supervisor"],
        },
        {
            "event": "started",
            "at": "2026-08-30T01:00:13+00:00",
            "process": "supervisor",
            "record": runtime["supervisor"],
        },
        {
            "event": "stopped",
            "at": "2026-08-30T01:00:14+00:00",
            "process": "api",
            "record": stage_a_api,
        },
        {
            "event": "started",
            "at": "2026-08-30T01:00:15+00:00",
            "process": "api",
            "record": runtime["api"],
        },
    ]
    run = {
        "schema": f"{e2e.SCHEMA}/evidence-run",
        "version": e2e.VERSION,
        "status": "passed",
        "run_id": path.name,
        "started_at": started_at,
        "completed_at": completed_at,
        "candidate_ref": "candidate-0123456789abcdef0123",
        "repositories": dict.fromkeys(("multirag", "ofmcp"), repository),
        "runtime_implementations": runtime,
        "runtime_boundary": {
            "started": runtime_started,
            "completed": runtime,
            "segments": restart_segments,
        },
        "waiting_restart": {
            "status": "complete",
            "started_at": "2026-08-30T01:00:09+00:00",
            "completed_at": "2026-08-30T01:00:16+00:00",
            "stage_a_overlay_sha256": stage_a_digest,
            "stage_b_overlay_sha256": stage_b_digest,
            "nonrestartable_before_sha256": e2e._nonrestartable_runtime_sha256(
                runtime_started,
            ),
            "nonrestartable_after_sha256": e2e._nonrestartable_runtime_sha256(
                runtime,
            ),
            "mock_before": {
                "pid": runtime["mock"]["pid"],
                "create_time": runtime["mock"]["create_time"],
                "do_create_request_blocked": 0,
                "calls": {},
            },
            "mock_after": {
                "pid": runtime["mock"]["pid"],
                "create_time": runtime["mock"]["create_time"],
                "do_create_request_blocked": 0,
                "calls": {},
            },
            "segments": restart_segments,
        },
        "migration_revisions": {"multirag": "head", "ofmcp_a6": e2e.A6_MIGRATION_HEAD},
        "deployment_manifest_sha256": digest,
        "selection_sha256": digest,
        "binding_generation": 1,
        "identity_revision": 1,
        "policy_revision": digest,
        "grant_revision": digest,
        "credential_generation": 1,
        "certificates": {
            "ca_sha256": digest,
            "issuer_sha256": digest,
            "gateway_sha256": digest,
        },
        "key_fingerprints": {
            "channel_secret_ring": {
                "active_key_id": "channel",
                "key_ids": ["channel"],
                "fingerprints": [digest],
            },
            "interaction_payload": {"fingerprint": digest},
            "p3_signing": {"kid": "p3", "public_key_fingerprint": digest},
            "ecology_simulator": {"public_key_fingerprint": digest},
            "ofmcp_request_state": {"fingerprints": [digest]},
            "ofmcp_a6_fingerprint": {"fingerprints": [digest]},
            "ofmcp_a6_identity": {"fingerprint": digest},
        },
        "artifact_sha256": {
            "api-stage-a.yaml": stage_a_digest,
            "api-stage-b.yaml": stage_b_digest,
            "jwks-public-keys.json": digest,
            "mcp-grants.json": digest,
            "tool-policies.json": digest,
        },
        "fixture": {
            "name": "ofmcp-ecology-mock",
            "write_enabled": False,
            "leave_contract_version": "2.0",
            "repository_head_sha": "a" * 40,
            "repository_state_sha256": repository["worktree_sha256"],
            "simulator_source_sha256": digest,
            "contract_snapshot_sha256": digest,
        },
        "trace_scope": "multirag_to_ofmcp",
        "cross_repository_trace_verified": True,
        "trace_ids": [first_trace, terminal_trace],
        "trace_id": terminal_trace,
        "a6_audit": {
            "logical_call_count": 2,
            "audit_event_count": 4,
            "write_tool_audit_event_count": 0,
            "sealed_end_recorded_seq": 14,
            "terminal_trace_id": terminal_trace,
            "trace_ids": [first_trace, terminal_trace],
            "first_recorded_seq": 11,
            "last_recorded_seq": 14,
        },
        "ecology_write_boundary": {
            "pid": 102,
            "create_time": 1.0,
            "blocked_at_start": 0,
            "blocked_at_end": 0,
            "calls_at_start": {},
            "calls_at_end": dict.fromkeys(e2e.ECOLOGY_READ_CALLS, 1),
            "read_call_deltas": dict.fromkeys(e2e.ECOLOGY_READ_CALLS, 1),
        },
    }
    checks = {
        "schema": f"{e2e.SCHEMA}/evidence-checks",
        "version": e2e.VERSION,
        "checks": [{"check_id": check_id, "status": "pass", "detail": "ok"} for check_id in sorted(e2e.LIVE_EVIDENCE_CHECK_IDS)],
    }
    e2e._write_json(path / "run.json", run)
    e2e._write_json(path / "checks.json", checks)
    e2e._write_json(
        path / "logs-safe.json",
        {
            "schema": f"{e2e.SCHEMA}/safe-log-projection",
            "version": e2e.VERSION,
            "processes": {
                name: {
                    "state": "running",
                    "pid": item["pid"],
                    "repo_head_sha": item["repo_head_sha"],
                    "repo_dirty": item["repo_dirty"],
                    "repo_state_sha256": item["repo_state_sha256"],
                }
                for name, item in runtime.items()
            },
            "delegated_trace_ids": [first_trace, terminal_trace],
        },
    )
    timeline = [
        {"event": "live_probe_started", "at": started_at, "run_id": path.name},
        *[
            {
                "event": f"runtime_process_{segment['event']}",
                "at": segment["at"],
                "process": segment["process"],
                "run_id": path.name,
            }
            for segment in restart_segments
        ],
        {"event": "interaction_completed", "at": completed_at, "run_id": path.name},
        {"event": "evidence_sealed", "at": completed_at, "run_id": path.name},
    ]
    e2e._atomic_write(
        path / "timeline.ndjson",
        b"".join(e2e._canonical_bytes(item) + b"\n" for item in timeline),
    )
    sums = "".join(f"{e2e._sha256_file(path / name)}  {name}\n" for name in ("checks.json", "logs-safe.json", "run.json", "timeline.ndjson"))
    e2e._atomic_write(path / "SHA256SUMS", sums.encode())


def _refresh_evidence_checksums(path: Path) -> None:
    names = ("checks.json", "logs-safe.json", "run.json", "timeline.ndjson")
    sums = "".join(f"{e2e._sha256_file(path / name)}  {name}\n" for name in names)
    e2e._atomic_write(path / "SHA256SUMS", sums.encode())


def test_evidence_verification_rejects_sensitive_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = (tmp_path / "root").resolve()
    layout = e2e._ensure_layout(root)
    bundle = layout.evidence / "eim-o5-20260830T010000Z-01234567"
    _write_evidence_fixture(bundle)
    monkeypatch.setattr(e2e, "_load_deployment", lambda _root: (layout, {}))
    monkeypatch.setattr(e2e, "_evidence_sensitive_values", lambda _layout: {b"forbidden-secret"})
    assert e2e.verify_evidence(bundle)["status"] == "verified"
    e2e._write_json(bundle / "logs-safe.json", {"message": "forbidden-secret"})
    sums = "".join(f"{e2e._sha256_file(bundle / name)}  {name}\n" for name in ("checks.json", "logs-safe.json", "run.json", "timeline.ndjson"))
    e2e._atomic_write(bundle / "SHA256SUMS", sums.encode())
    with pytest.raises(e2e.OperatorError) as error:
        e2e.verify_evidence(bundle)
    assert error.value.detail == "evidence_sensitive"


def test_evidence_verification_rejects_extra_safe_log_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = (tmp_path / "root").resolve()
    layout = e2e._ensure_layout(root)
    bundle = layout.evidence / "eim-o5-20260830T010000Z-89abcdef"
    _write_evidence_fixture(bundle)
    monkeypatch.setattr(e2e, "_load_deployment", lambda _root: (layout, {}))
    monkeypatch.setattr(e2e, "_evidence_sensitive_values", lambda _layout: set())
    logs = e2e._read_json(bundle / "logs-safe.json")
    processes = cast(dict[str, dict[str, object]], logs["processes"])
    processes["api"]["argv_sha256"] = "f" * 64
    e2e._write_json(bundle / "logs-safe.json", logs)
    sums = "".join(
        f"{e2e._sha256_file(bundle / name)}  {name}\n"
        for name in (
            "checks.json",
            "logs-safe.json",
            "run.json",
            "timeline.ndjson",
        )
    )
    e2e._atomic_write(bundle / "SHA256SUMS", sums.encode())

    with pytest.raises(e2e.OperatorError) as error:
        e2e.verify_evidence(bundle)

    assert error.value.detail == "evidence_logs"


def test_evidence_verification_binds_restart_overlay_to_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    bundle = layout.evidence / "eim-o5-20260830T010000Z-11111111"
    _write_evidence_fixture(bundle)
    monkeypatch.setattr(e2e, "_load_deployment", lambda _root: (layout, {}))
    monkeypatch.setattr(e2e, "_evidence_sensitive_values", lambda _layout: set())
    run = e2e._read_json(bundle / "run.json")
    cast(dict[str, object], run["waiting_restart"])["stage_a_overlay_sha256"] = "f" * 64
    e2e._write_json(bundle / "run.json", run)
    _refresh_evidence_checksums(bundle)

    with pytest.raises(e2e.OperatorError) as error:
        e2e.verify_evidence(bundle)

    assert error.value.detail == "evidence_waiting_restart"


def test_evidence_verification_rejects_restart_outside_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    bundle = layout.evidence / "eim-o5-20260830T010000Z-22222222"
    _write_evidence_fixture(bundle)
    monkeypatch.setattr(e2e, "_load_deployment", lambda _root: (layout, {}))
    monkeypatch.setattr(e2e, "_evidence_sensitive_values", lambda _layout: set())
    run = e2e._read_json(bundle / "run.json")
    restart = cast(dict[str, object], run["waiting_restart"])
    restart["started_at"] = "2030-08-30T01:00:09+00:00"
    restart["completed_at"] = "2030-08-30T01:00:16+00:00"
    e2e._write_json(bundle / "run.json", run)
    _refresh_evidence_checksums(bundle)

    with pytest.raises(e2e.OperatorError) as error:
        e2e.verify_evidence(bundle)

    assert error.value.detail == "evidence_waiting_restart"


def test_evidence_verification_rejects_reused_restart_process_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    bundle = layout.evidence / "eim-o5-20260830T010000Z-33333333"
    _write_evidence_fixture(bundle)
    monkeypatch.setattr(e2e, "_load_deployment", lambda _root: (layout, {}))
    monkeypatch.setattr(e2e, "_evidence_sensitive_values", lambda _layout: set())
    run = e2e._read_json(bundle / "run.json")
    segments = cast(
        list[dict[str, object]],
        cast(dict[str, object], run["runtime_boundary"])["segments"],
    )
    restart_segments = cast(
        list[dict[str, object]],
        cast(dict[str, object], run["waiting_restart"])["segments"],
    )
    for target in (segments, restart_segments):
        initial_api = cast(dict[str, object], target[0]["record"])
        for index in (1, 4):
            cast(dict[str, object], target[index]["record"])["pid"] = initial_api["pid"]
            cast(dict[str, object], target[index]["record"])["process_group"] = initial_api["process_group"]
            cast(dict[str, object], target[index]["record"])["create_time"] = initial_api["create_time"]
    e2e._write_json(bundle / "run.json", run)
    _refresh_evidence_checksums(bundle)

    with pytest.raises(e2e.OperatorError) as error:
        e2e.verify_evidence(bundle)

    assert error.value.detail == "evidence_waiting_restart"


def test_evidence_verification_rejects_mock_change_during_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    bundle = layout.evidence / "eim-o5-20260830T010000Z-44444444"
    _write_evidence_fixture(bundle)
    monkeypatch.setattr(e2e, "_load_deployment", lambda _root: (layout, {}))
    monkeypatch.setattr(e2e, "_evidence_sensitive_values", lambda _layout: set())
    run = e2e._read_json(bundle / "run.json")
    restart = cast(dict[str, object], run["waiting_restart"])
    mock_after = cast(dict[str, object], restart["mock_after"])
    mock_after["calls"] = {"applytoken": 1}
    e2e._write_json(bundle / "run.json", run)
    _refresh_evidence_checksums(bundle)

    with pytest.raises(e2e.OperatorError) as error:
        e2e.verify_evidence(bundle)

    assert error.value.detail == "evidence_waiting_restart"
