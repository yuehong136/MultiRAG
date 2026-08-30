"""EIM-O5 / CHN-O16 secure leave-preview local vertical-slice operator.

This command deliberately keeps deployment material outside both repositories.
It never creates Channel bindings, enterprise identities, or Canvas releases;
``discover`` validates those existing authorities and fails closed when they are
missing or ambiguous.

The live topology is host based (macOS/Linux).  PostgreSQL/Redis may run in
containers, while the API, Channel supervisor, JWKS publisher, secure of_mcp
Gateway, and Ecology simulator remain loopback-only host processes.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import fcntl
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import ssl
import stat
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any, NoReturn, Protocol, cast
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import psutil
import yaml
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import BaseModel, SecretStr

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "com.multirag/secure-leave-e2e"
VERSION = 1
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DEFAULT_PORTS = {
    "api": 8123,
    "gateway": 8765,
    "jwks": 9277,
    "mock": 18765,
}
RESOURCE_NAME = "ofmcp_gateway"
P3_KEY_ID = "eim-o5-p3-v1"
A6_MIGRATION_HEAD = "0001_a6_security_ledger"
CHANNEL_KEY_ENV = "MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY"
CHANNEL_TOKEN_ENV = "MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN"
CHANNEL_PROVISIONING_ENVS = frozenset(
    {
        "MULTIRAG_IDENTITY__PROVISIONING__ACTIVE_KEY_ID",
        "MULTIRAG_IDENTITY__PROVISIONING__HMAC_KEYRING",
    },
)
FIXED_LIVE_PROMPT = "帮我做一次请假试算，只生成预览，不创建草稿、不提交审批。缺少的信息请通过飞书原生表单向我收集。"
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "bearer",
        "ciphertext",
        "dsn",
        "jti",
        "password",
        "private_key",
        "raw_id",
        "secret",
        "subject",
        "token",
    },
)
PROCESS_ORDER = ("jwks", "api", "mock", "gateway", "supervisor")
RESTARTABLE_DURING_PROBE = frozenset({"api", "supervisor"})
ECOLOGY_READ_CALLS = frozenset(
    {
        "applytoken",
        "loadForm",
        "reqDataInputResult",
        "getDBFormulaValue",
        "getVacationInfo",
        "getLeaveWorkDuration",
    },
)
LEAVE_FORM_FIELDS = frozenset(
    {
        "leave_type",
        "start_date",
        "start_hour",
        "start_minute",
        "end_date",
        "end_hour",
        "end_minute",
    },
)
LIVE_EVIDENCE_CHECK_IDS = frozenset(
    {
        "live.interaction.exactly_once",
        "live.callback.claimed_once",
        "live.resume.succeeded_once",
        "live.terminal.delivered",
        "live.form.seven_fields",
        "live.terminal.strict_v2_projection",
        "live.identity.authority_bound",
        "live.policy.grant_bound",
        "live.ecology.write_calls_zero",
        "live.a6.stage_pairs_correlated",
        "live.trace.cross_repository",
        "live.runtime.segment_ledger_verified",
        "live.runtime.wait_restart_verified",
        "live.runtime.implementation_state_recorded",
        "live.migrations.revisions_recorded",
    },
)
ROLLBACK_ORDER = ("api_disable_producer", "supervisor", "gateway", "mock", "jwks", "api")
UP_STAGE_ORDER = (
    "inventory",
    "migrations",
    "authority_prepare",
    "jwks",
    "api_stage_a",
    "mock",
    "gateway",
    "supervisor_consumer",
    "api_stage_b",
    "online_doctor",
)


class ExitCode(StrEnum):
    OK = "OK"
    ARGUMENT_INVALID = "ARGUMENT_INVALID"
    ROOT_INVALID = "ROOT_INVALID"
    PREREQUISITE_MISSING = "PREREQUISITE_MISSING"
    AUTHORITY_AMBIGUOUS = "AUTHORITY_AMBIGUOUS"
    AUTHORITY_STALE = "AUTHORITY_STALE"
    PORT_IN_USE = "PORT_IN_USE"
    PROCESS_NOT_OWNED = "PROCESS_NOT_OWNED"
    PROCESS_FAILED = "PROCESS_FAILED"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    LIVE_EVIDENCE_PENDING = "LIVE_EVIDENCE_PENDING"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class OperatorError(RuntimeError):
    """Bounded error carrying only a stable operator-safe code."""

    def __init__(self, code: ExitCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__("secure leave operation rejected")


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise OperatorError(ExitCode.ARGUMENT_INVALID)


@dataclass(frozen=True, slots=True)
class Layout:
    root: Path
    secrets: Path
    pki: Path
    artifacts: Path
    run: Path
    logs: Path
    evidence: Path
    deployment: Path
    selection: Path

    @classmethod
    def from_root(cls, root: Path) -> Layout:
        return cls(
            root=root,
            secrets=root / "secrets",
            pki=root / "pki",
            artifacts=root / "artifacts",
            run=root / "run",
            logs=root / "logs",
            evidence=root / "evidence",
            deployment=root / "deployment.json",
            selection=root / "selection.json",
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    ref: str
    tenant_id: str = field(repr=False)
    platform_user_id: str = field(repr=False)
    provider_tenant: str = field(repr=False)
    provider_account_id: str = field(repr=False)
    external_identity_id: str = field(repr=False)
    identity_revision: int = field(repr=False)
    channel_id: str = field(repr=False)
    binding_id: str = field(repr=False)
    binding_generation: int
    agent_id: str
    agent_revision_id: str
    mcp_server_id: str

    def sensitive_document(self) -> dict[str, object]:
        return asdict(self)

    def safe_document(self) -> dict[str, object]:
        return {
            "candidate_ref": self.ref,
            "binding_generation": self.binding_generation,
        }


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    name: str
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    port: int | None = None


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    name: str
    pid: int
    create_time: float
    cwd: str
    argv_sha256: str
    process_group: int
    repo_head_sha: str
    repo_dirty: bool
    repo_state_sha256: str
    api_stage: str | None = None
    overlay_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class WaitingRestartOutcome:
    response: Mapping[str, object]
    runtime_before: Mapping[str, object]
    runtime_after: Mapping[str, object]
    mock_before: Mapping[str, object]
    mock_after: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Check:
    check_id: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            capture_output=True,
            check=False,
            text=True,
            timeout=120,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _require_current_owner(path: Path, *, detail: str) -> None:
    if os.name != "nt" and path.lstat().st_uid != os.geteuid():
        raise OperatorError(ExitCode.ROOT_INVALID, detail)


def _ensure_supported_platform() -> None:
    if platform.system() not in {"Darwin", "Linux"}:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "platform")


def _validate_root(root: Path, *, ofmcp_repo: Path | None = None) -> Path:
    _ensure_supported_platform()
    if not root.is_absolute() or root == Path("/") or root == Path.home():
        raise OperatorError(ExitCode.ROOT_INVALID, "broad_path")
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise OperatorError(ExitCode.ROOT_INVALID, "root_type")
    resolved = root.resolve(strict=False)
    forbidden = [REPOSITORY_ROOT.resolve()]
    if ofmcp_repo is not None:
        forbidden.append(ofmcp_repo.resolve())
    for repo in forbidden:
        if resolved == repo or repo in resolved.parents or resolved in repo.parents:
            raise OperatorError(ExitCode.ROOT_INVALID, "inside_repository")
    return resolved


def _assert_regular(path: Path, *, mode: int | None = None, nonempty: bool = True) -> None:
    if path.is_symlink() or not path.is_file():
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "file_type")
    if os.name != "nt" and path.lstat().st_uid != os.geteuid():
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "file_owner")
    if nonempty and path.stat().st_size <= 0:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "empty_file")
    if mode is not None and os.name != "nt" and _path_mode(path) != mode:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "file_mode")


def _ensure_layout(root: Path) -> Layout:
    layout = Layout.from_root(root)
    if root.exists():
        _require_current_owner(root, detail="root_owner")
        if _path_mode(root) != 0o700:
            raise OperatorError(ExitCode.ROOT_INVALID, "root_mode")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    _require_current_owner(root, detail="root_owner")
    os.chmod(root, 0o700)
    for directory in (
        layout.secrets,
        layout.pki,
        layout.artifacts,
        layout.run,
        layout.logs,
        layout.evidence,
    ):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise OperatorError(ExitCode.ROOT_INVALID, "layout_type")
        directory.mkdir(mode=0o700, exist_ok=True)
        _require_current_owner(directory, detail="layout_owner")
        if _path_mode(directory) != 0o700:
            raise OperatorError(ExitCode.ROOT_INVALID, "layout_mode")
    return layout


def _atomic_write(path: Path, value: bytes, *, mode: int = 0o600) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "write_target")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    _atomic_write(path, _canonical_bytes(value) + b"\n", mode=mode)


def _append_json_line(path: Path, value: object) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "timeline_path")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(descriptor, _canonical_bytes(value) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _record_stage(layout: Layout, stage: str, status: str) -> None:
    if stage not in UP_STAGE_ORDER or status not in {"begin", "complete", "failed"}:
        raise OperatorError(ExitCode.ARGUMENT_INVALID, "stage_event")
    _append_json_line(
        layout.run / "timeline.ndjson",
        {"event": "stage", "stage": stage, "status": status, "at": _now()},
    )


@contextmanager
def _operation_lock(layout: Layout) -> Generator[None, None, None]:
    repository_digest = hashlib.sha256(str(REPOSITORY_ROOT.resolve()).encode()).hexdigest()[:24]
    paths = (
        Path(tempfile.gettempdir()) / f"multirag-secure-leave-{repository_digest}.lock",
        layout.run / "operation.lock",
    )
    descriptors: list[int] = []
    try:
        for path in paths:
            try:
                descriptor = os.open(
                    path,
                    os.O_RDWR | os.O_CREAT | _O_NOFOLLOW,
                    0o600,
                )
            except OSError as exc:
                raise OperatorError(ExitCode.PROCESS_FAILED, "operation_lock") from exc
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                os.close(descriptor)
                raise OperatorError(ExitCode.PROCESS_FAILED, "operation_lock_owner")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                raise OperatorError(ExitCode.PROCESS_FAILED, "operation_in_progress") from exc
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _read_json(path: Path, *, mode: int = 0o600) -> dict[str, Any]:
    _assert_regular(path, mode=mode)
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "json") from exc
    if type(value) is not dict:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "json_shape")
    return cast(dict[str, Any], value)


def _secret_base64(byte_count: int = 32) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(byte_count)).rstrip(b"=").decode("ascii")


def _read_env_file(path: Path) -> dict[str, str]:
    _assert_regular(path, mode=0o600)
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name or name in result:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "env_shape")
        result[name] = value
    return result


def _canonical_channel_secrets_dir() -> Path:
    configured = os.environ.get("MULTIRAG_SECRETS_DIR")
    if configured:
        candidate = Path(configured).expanduser()
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        candidate = Path(xdg_config).expanduser() / "multirag" / "secrets" if xdg_config else Path.home() / ".config" / "multirag" / "secrets"
    if not candidate.is_absolute():
        raise OperatorError(ExitCode.ROOT_INVALID, "canonical_channel_secrets")
    return candidate.resolve(strict=False)


def _channel_key_ring(encoded: str) -> tuple[bytes, ...]:
    from common.channel_secret_crypto import ChannelSecretCipherError, decode_channel_secret_key

    try:
        parsed = yaml.safe_load(encoded)
    except yaml.YAMLError as exc:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_key_ring") from exc
    raw_items: list[object]
    if isinstance(parsed, str):
        raw_items = [parsed]
    elif isinstance(parsed, list):
        raw_items = parsed
    else:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_key_ring")
    if not 1 <= len(raw_items) <= 16 or any(not isinstance(item, str) for item in raw_items):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_key_ring")
    try:
        keys = tuple(decode_channel_secret_key(cast(str, item)) for item in raw_items)
    except ChannelSecretCipherError as exc:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_key_ring") from exc
    key_ids = [_sha256_bytes(key)[:16] for key in keys]
    if len(set(key_ids)) != len(key_ids):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_key_ring_duplicate")
    return keys


async def _stored_channel_secret_count() -> int:
    from sqlalchemy import func, select

    from api.db.db_models import ChannelSecret, async_session_factory
    from common.bootstrap import ensure_initialized

    ensure_initialized(initialize_resources=False)
    if async_session_factory is None:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "database")
    async with async_session_factory() as session:
        value = await session.scalar(select(func.count()).select_from(ChannelSecret))
    if type(value) is not int or value < 0:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "channel_secret_inventory")
    return value


def _validate_channel_api_env(values: Mapping[str, str]) -> None:
    required = {CHANNEL_KEY_ENV, CHANNEL_TOKEN_ENV}
    optional = set(values) & CHANNEL_PROVISIONING_ENVS
    if not required <= set(values) or set(values) - required - CHANNEL_PROVISIONING_ENVS or optional not in (set(), set(CHANNEL_PROVISIONING_ENVS)) or any(not value for value in values.values()):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_secret_shape")
    try:
        token = values[CHANNEL_TOKEN_ENV]
        padded = token + "=" * (-len(token) % 4)
        token_bytes = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (KeyError, ValueError) as exc:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_control_token") from exc
    if len(token_bytes) < 32 or not secrets.compare_digest(
        base64.urlsafe_b64encode(token_bytes).rstrip(b"=").decode("ascii"),
        token,
    ):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_control_token")
    if optional:
        from common.app_config import IdentityProvisioningConfig

        try:
            provisioning = IdentityProvisioningConfig.model_validate(
                {
                    "active_key_id": values["MULTIRAG_IDENTITY__PROVISIONING__ACTIVE_KEY_ID"],
                    "hmac_keyring": yaml.safe_load(
                        values["MULTIRAG_IDENTITY__PROVISIONING__HMAC_KEYRING"],
                    ),
                },
            )
            provisioning.require_hmac_keyring()
        except Exception as exc:
            raise OperatorError(
                ExitCode.ARTIFACT_INVALID,
                "provisioning_keyring",
            ) from exc


def _channel_env_document(values: Mapping[str, str]) -> bytes:
    _validate_channel_api_env(values)
    ordered_names = [CHANNEL_KEY_ENV, CHANNEL_TOKEN_ENV, *sorted(CHANNEL_PROVISIONING_ENVS & set(values))]
    return "".join(f"{name}={values[name]}\n" for name in ordered_names).encode("utf-8")


def _supervisor_env_document(*, token: str, api_port: int) -> bytes:
    return (f"MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL=http://127.0.0.1:{api_port}\nMULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN={token}\n").encode()


def _p3_keygen_argv(p3_dir: Path) -> tuple[str, ...]:
    return (
        "sh",
        str(REPOSITORY_ROOT / "scripts/init_mcp_signing_key.example.sh"),
        "--kid",
        P3_KEY_ID,
        "--key-dir",
        str(p3_dir),
    )


def _safe_command(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> None:
    result = runner.run(argv, cwd=cwd, env=env)
    if result.returncode != 0:
        raise OperatorError(ExitCode.PROCESS_FAILED, "command")


def _generate_pki(layout: Layout, runner: CommandRunner) -> dict[str, object]:
    if shutil.which("openssl") is None:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "openssl")
    paths = {
        "ca_key": layout.pki / "ca-key.pem",
        "ca_cert": layout.pki / "ca.pem",
        "issuer_key": layout.pki / "issuer-key.pem",
        "issuer_cert": layout.pki / "issuer-cert.pem",
        "gateway_key": layout.pki / "gateway-key.pem",
        "gateway_cert": layout.pki / "gateway-cert.pem",
    }
    existing = [path.exists() or path.is_symlink() for path in paths.values()]
    if any(existing) and not all(existing):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "partial_pki")
    if not all(existing):
        _safe_command(
            runner,
            ["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-out", str(paths["ca_key"])],
            cwd=layout.pki,
        )
        _safe_command(
            runner,
            [
                "openssl",
                "req",
                "-x509",
                "-new",
                "-sha256",
                "-days",
                "365",
                "-key",
                str(paths["ca_key"]),
                "-subj",
                "/CN=MultiRAG EIM-O5 Local CA",
                "-out",
                str(paths["ca_cert"]),
            ],
            cwd=layout.pki,
        )
        extension = layout.pki / ".leaf-ext.cnf"
        _atomic_write(
            extension,
            b"subjectAltName=IP:127.0.0.1,DNS:localhost\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature\n",
        )
        try:
            for name in ("issuer", "gateway"):
                request = layout.pki / f".{name}.csr.pem"
                _safe_command(
                    runner,
                    [
                        "openssl",
                        "genpkey",
                        "-algorithm",
                        "EC",
                        "-pkeyopt",
                        "ec_paramgen_curve:prime256v1",
                        "-out",
                        str(paths[f"{name}_key"]),
                    ],
                    cwd=layout.pki,
                )
                _safe_command(
                    runner,
                    [
                        "openssl",
                        "req",
                        "-new",
                        "-key",
                        str(paths[f"{name}_key"]),
                        "-subj",
                        f"/CN=MultiRAG EIM-O5 {name.title()}",
                        "-out",
                        str(request),
                    ],
                    cwd=layout.pki,
                )
                serial = secrets.token_hex(16)
                _safe_command(
                    runner,
                    [
                        "openssl",
                        "x509",
                        "-req",
                        "-sha256",
                        "-days",
                        "30",
                        "-in",
                        str(request),
                        "-CA",
                        str(paths["ca_cert"]),
                        "-CAkey",
                        str(paths["ca_key"]),
                        "-set_serial",
                        f"0x{serial}",
                        "-extfile",
                        str(extension),
                        "-out",
                        str(paths[f"{name}_cert"]),
                    ],
                    cwd=layout.pki,
                )
                request.unlink()
        finally:
            if extension.exists():
                extension.unlink()
        for key in (paths["ca_key"], paths["issuer_key"], paths["gateway_key"]):
            os.chmod(key, 0o600)
        for cert in (paths["ca_cert"], paths["issuer_cert"], paths["gateway_cert"]):
            os.chmod(cert, 0o644)
    return _validate_pki(paths)


def _validate_pki(paths: Mapping[str, Path]) -> dict[str, object]:
    for name, path in paths.items():
        _assert_regular(path, mode=0o600 if name.endswith("key") else 0o644)
    try:
        ca = x509.load_pem_x509_certificate(paths["ca_cert"].read_bytes())
        ca_key = serialization.load_pem_private_key(paths["ca_key"].read_bytes(), password=None)
        ca_public = ca.public_key()
        if (
            not isinstance(ca_key, ec.EllipticCurvePrivateKey)
            or not isinstance(ca_key.curve, ec.SECP256R1)
            or not isinstance(ca_public, ec.EllipticCurvePublicKey)
            or not isinstance(ca_public.curve, ec.SECP256R1)
        ):
            raise ValueError
        if ca_key.public_key().public_numbers() != ca_public.public_numbers():
            raise ValueError
        ca_constraints = ca.extensions.get_extension_for_class(x509.BasicConstraints).value
        ca_hash = ca.signature_hash_algorithm
        if not ca_constraints.ca or ca.issuer != ca.subject or not isinstance(ca_hash, hashes.SHA256):
            raise ValueError
        ca_public.verify(
            ca.signature,
            ca.tbs_certificate_bytes,
            ec.ECDSA(ca_hash),
        )
        now = datetime.now(UTC)
        if not ca.not_valid_before_utc <= now < ca.not_valid_after_utc:
            raise ValueError
        if (ca.not_valid_after_utc - ca.not_valid_before_utc).days > 367:
            raise ValueError
        leaves: dict[str, object] = {}
        for name in ("issuer", "gateway"):
            cert = x509.load_pem_x509_certificate(paths[f"{name}_cert"].read_bytes())
            key = serialization.load_pem_private_key(paths[f"{name}_key"].read_bytes(), password=None)
            cert_public = cert.public_key()
            if (
                not isinstance(key, ec.EllipticCurvePrivateKey)
                or not isinstance(key.curve, ec.SECP256R1)
                or not isinstance(cert_public, ec.EllipticCurvePublicKey)
                or not isinstance(cert_public.curve, ec.SECP256R1)
            ):
                raise ValueError
            if key.public_key().public_numbers() != cert_public.public_numbers():
                raise ValueError
            cert_hash = cert.signature_hash_algorithm
            if cert.issuer != ca.subject or not isinstance(cert_hash, hashes.SHA256):
                raise ValueError
            ca_public.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                ec.ECDSA(cert_hash),
            )
            sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            dns_names = set(sans.get_values_for_type(x509.DNSName))
            ip_names = {str(item) for item in sans.get_values_for_type(x509.IPAddress)}
            usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            key_usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
            if (
                dns_names != {"localhost"}
                or ip_names != {"127.0.0.1"}
                or set(usage) != {x509.oid.ExtendedKeyUsageOID.SERVER_AUTH}
                or not key_usage.digital_signature
                or key_usage.key_cert_sign
                or not cert.not_valid_before_utc <= now < cert.not_valid_after_utc
                or (cert.not_valid_after_utc - cert.not_valid_before_utc).days > 32
            ):
                raise ValueError
            leaves[name] = {
                "cert_file": str(paths[f"{name}_cert"]),
                "key_file": str(paths[f"{name}_key"]),
                "sha256": _sha256_file(paths[f"{name}_cert"]),
                "not_after": cert.not_valid_after_utc.isoformat(),
            }
    except (
        ValueError,
        TypeError,
        x509.ExtensionNotFound,
        InvalidSignature,
    ) as exc:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "pki") from exc
    return {
        "ca": {
            "cert_file": str(paths["ca_cert"]),
            "sha256": _sha256_file(paths["ca_cert"]),
            "not_after": ca.not_valid_after_utc.isoformat(),
        },
        "leaves": leaves,
    }


def _validate_p3_pair(private_path: Path, public_path: Path) -> str:
    _assert_regular(private_path, mode=0o600)
    _assert_regular(public_path)
    try:
        private = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        public = serialization.load_pem_public_key(public_path.read_bytes())
    except ValueError as exc:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "p3_key") from exc
    if (
        not isinstance(private, ec.EllipticCurvePrivateKey)
        or not isinstance(private.curve, ec.SECP256R1)
        or not isinstance(public, ec.EllipticCurvePublicKey)
        or not isinstance(public.curve, ec.SECP256R1)
    ):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "p3_key_type")
    if private.public_key().public_numbers() != public.public_numbers():
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "p3_key_pair")
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return _sha256_bytes(der)


def _validate_mock_pair(private_path: Path, public_spk_path: Path) -> str:
    _assert_regular(private_path, mode=0o600)
    _assert_regular(public_spk_path)
    try:
        private = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        if not isinstance(private, rsa.RSAPrivateKey) or private.key_size < 2048:
            raise ValueError
        derived = base64.b64encode(
            private.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ),
        ).decode("ascii")
        stored = public_spk_path.read_text(encoding="ascii").strip()
    except (UnicodeError, ValueError) as exc:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "mock_key") from exc
    if not secrets.compare_digest(derived, stored):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "mock_key_pair")
    return _sha256_bytes(base64.b64decode(stored))


def _bootstrap_dependencies(
    layout: Layout,
    *,
    ofmcp_repo: Path,
    api_port: int,
    runner: CommandRunner,
) -> dict[str, object]:
    channel_dir = layout.secrets / "channel"
    channel_dir.mkdir(mode=0o700, exist_ok=True)
    api_env = channel_dir / "api.env"
    supervisor_env = channel_dir / "supervisor.env"
    if api_env.exists() != supervisor_env.exists():
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "partial_channel_secrets")
    canonical_dir = _canonical_channel_secrets_dir()
    canonical_api = canonical_dir / "api.env"
    canonical_supervisor = canonical_dir / "supervisor.env"
    canonical_present = canonical_api.exists() or canonical_supervisor.exists()
    if canonical_present and canonical_api.exists() != canonical_supervisor.exists():
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "partial_canonical_channel_secrets")
    if not api_env.exists() and canonical_present and canonical_dir != channel_dir.resolve():
        canonical_api_values = _read_env_file(canonical_api)
        canonical_supervisor_values = _read_env_file(canonical_supervisor)
        key_name = CHANNEL_KEY_ENV
        token_name = CHANNEL_TOKEN_ENV
        _validate_channel_api_env(canonical_api_values)
        if (
            set(canonical_supervisor_values)
            != {
                token_name,
                "MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL",
            }
            or canonical_api_values[token_name] != canonical_supervisor_values[token_name]
        ):
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "canonical_channel_secret_shape")
        _channel_key_ring(canonical_api_values[key_name])
        _atomic_write(
            api_env,
            _channel_env_document(canonical_api_values),
        )
        _atomic_write(
            supervisor_env,
            _supervisor_env_document(
                token=canonical_api_values[token_name],
                api_port=api_port,
            ),
        )
    elif not api_env.exists():
        try:
            stored_secret_count = asyncio.run(_stored_channel_secret_count())
        except OperatorError:
            raise
        except Exception as exc:
            raise OperatorError(ExitCode.PREREQUISITE_MISSING, "channel_secret_inventory") from exc
        if stored_secret_count:
            raise OperatorError(ExitCode.PREREQUISITE_MISSING, "channel_decryption_key")
        environment = _base_process_env()
        environment["MULTIRAG_SECRETS_DIR"] = str(channel_dir)
        _safe_command(
            runner,
            [
                "sh",
                str(REPOSITORY_ROOT / "scripts/init_channel_secrets.example.sh"),
                f"http://127.0.0.1:{api_port}",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
        )
    channel_api = _read_env_file(api_env)
    channel_supervisor = _read_env_file(supervisor_env)
    _validate_channel_api_env(channel_api)
    if set(channel_supervisor) != {
        CHANNEL_TOKEN_ENV,
        "MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL",
    }:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_secret_shape")
    if channel_api[CHANNEL_TOKEN_ENV] != channel_supervisor[CHANNEL_TOKEN_ENV]:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_token_pair")
    if canonical_present and canonical_dir != channel_dir.resolve():
        canonical_api_values = _read_env_file(canonical_api)
        if canonical_api_values != channel_api:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_secret_source_drift")
    expected_api_url = f"http://127.0.0.1:{api_port}"
    if channel_supervisor["MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL"] != expected_api_url:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_api_port_drift")
    channel_keys = _channel_key_ring(
        channel_api["MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY"],
    )
    channel_key_ids = [_sha256_bytes(key)[:16] for key in channel_keys]

    interaction_path = layout.secrets / "interaction-payload-key.txt"
    if interaction_path.exists():
        _assert_regular(interaction_path, mode=0o600)
        interaction_key = interaction_path.read_text(encoding="ascii").strip()
    else:
        interaction_key = _secret_base64()
        _atomic_write(interaction_path, (interaction_key + "\n").encode("ascii"))
    from common.mcp_interactions import decode_interaction_payload_key

    try:
        interaction_raw = decode_interaction_payload_key(interaction_key)
    except ValueError as exc:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "interaction_key") from exc

    p3_dir = layout.secrets / "p3"
    p3_dir.mkdir(mode=0o700, exist_ok=True)
    p3_private = p3_dir / f"{P3_KEY_ID}-private.pem"
    p3_public = p3_dir / f"{P3_KEY_ID}-public.pem"
    if p3_private.exists() != p3_public.exists():
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "partial_p3_key")
    if not p3_private.exists():
        _safe_command(
            runner,
            _p3_keygen_argv(p3_dir),
            cwd=REPOSITORY_ROOT,
        )
    p3_fingerprint = _validate_p3_pair(p3_private, p3_public)

    mock_dir = layout.secrets / "ecology-mock"
    mock_dir.mkdir(mode=0o700, exist_ok=True)
    mock_private = mock_dir / "private.pem"
    mock_spk = mock_dir / "public-spk.txt"
    if mock_private.exists() != mock_spk.exists():
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "partial_mock_key")
    if not mock_private.exists():
        _safe_command(
            runner,
            [
                "uv",
                "run",
                "ofmcp-ecology-mock",
                "keygen",
                "--private-key",
                str(mock_private),
                "--public-spk",
                str(mock_spk),
            ],
            cwd=ofmcp_repo,
        )
    mock_fingerprint = _validate_mock_pair(mock_private, mock_spk)

    _safe_command(
        runner,
        ["uv", "run", "ofmcp", "ops", "bootstrap", "--root", str(layout.root), "--format", "json"],
        cwd=ofmcp_repo,
    )
    ofmcp_manifest = _read_json(layout.root / "ofmcp-deployment.json")
    if ofmcp_manifest.get("schema") != "com.ofmcp/ops-deployment" or ofmcp_manifest.get("version") != 1:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "ofmcp_manifest")

    a6_dsn_path = layout.secrets / "ofmcp-a6-dsn.txt"
    from common.app_config import get_app_config

    postgres = get_app_config().postgresql
    if a6_dsn_path.exists():
        _assert_regular(a6_dsn_path, mode=0o600)
        a6_dsn = a6_dsn_path.read_text(encoding="utf-8").strip()
        if not a6_dsn:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "a6_dsn")
    else:
        if postgres.dbname == "ofmcp_a6":
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "a6_database_not_distinct")
        a6_dsn = _postgres_dsn(
            host=postgres.host,
            port=postgres.port,
            user=postgres.user,
            password=postgres.password,
            database="ofmcp_a6",
        )
        _atomic_write(
            a6_dsn_path,
            (a6_dsn + "\n").encode("utf-8"),
        )
    if _effective_postgres_dbname(a6_dsn) == postgres.dbname:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "a6_database_not_distinct")

    return {
        "channel": {
            "api_env_file": str(api_env),
            "supervisor_env_file": str(supervisor_env),
            "active_key_id": channel_key_ids[0],
            "key_ids": channel_key_ids,
            "key_fingerprints": [_sha256_bytes(key) for key in channel_keys],
        },
        "interaction": {
            "key_file": str(interaction_path),
            "key_fingerprint": _sha256_bytes(interaction_raw),
        },
        "p3": {
            "kid": P3_KEY_ID,
            "private_key_file": str(p3_private),
            "public_key_file": str(p3_public),
            "public_key_fingerprint": p3_fingerprint,
        },
        "ecology_mock": {
            "private_key_file": str(mock_private),
            "public_spk_file": str(mock_spk),
            "public_key_fingerprint": mock_fingerprint,
        },
        "ofmcp_manifest": str(layout.root / "ofmcp-deployment.json"),
        "a6_dsn_file": str(a6_dsn_path),
    }


def bootstrap(
    root: Path,
    *,
    ofmcp_repo: Path,
    ports: Mapping[str, int],
    runner: CommandRunner | None = None,
    _lock: bool = True,
) -> dict[str, object]:
    ofmcp_repo = ofmcp_repo.resolve()
    if not (ofmcp_repo / "AGENTS.md").is_file():
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "ofmcp_repo")
    root = _validate_root(root, ofmcp_repo=ofmcp_repo)
    layout = _ensure_layout(root)
    if _lock:
        with _operation_lock(layout):
            return bootstrap(
                root,
                ofmcp_repo=ofmcp_repo,
                ports=ports,
                runner=runner,
                _lock=False,
            )
    if len(set(ports.values())) != len(ports) or set(ports) != set(DEFAULT_PORTS) or any(not 1 <= value <= 65535 for value in ports.values()):
        raise OperatorError(ExitCode.ARGUMENT_INVALID, "ports")
    command_runner = runner or SubprocessCommandRunner()
    pki = _generate_pki(layout, command_runner)
    secrets_manifest = _bootstrap_dependencies(
        layout,
        ofmcp_repo=ofmcp_repo,
        api_port=ports["api"],
        runner=command_runner,
    )
    manifest: dict[str, object] = {
        "schema": SCHEMA,
        "version": VERSION,
        "root": str(root),
        "platform": platform.system().lower(),
        "created_or_verified_at": _now(),
        "repositories": {
            "multirag": str(REPOSITORY_ROOT),
            "ofmcp": str(ofmcp_repo),
        },
        "ports": dict(sorted(ports.items())),
        "urls": {
            "api": f"http://127.0.0.1:{ports['api']}",
            "gateway": f"https://127.0.0.1:{ports['gateway']}/mcp",
            "issuer": f"https://127.0.0.1:{ports['jwks']}",
            "jwks": f"https://127.0.0.1:{ports['jwks']}/.well-known/jwks.json",
            "mock": f"http://127.0.0.1:{ports['mock']}",
        },
        "pki": pki,
        "secrets": secrets_manifest,
    }
    if layout.deployment.exists():
        previous = _read_json(layout.deployment)
        stable_previous = {key: value for key, value in previous.items() if key != "created_or_verified_at"}
        stable_current = {key: value for key, value in manifest.items() if key != "created_or_verified_at"}
        if stable_previous != stable_current:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "deployment_drift")
        # Preserve the original bytes and timestamp.  A successful bootstrap
        # rerun must not change the manifest digest merely because validation
        # happened at a later wall-clock time.
        return {
            "status": "ready",
            "root": str(root),
            "manifest_sha256": _sha256_file(layout.deployment),
            "ports": dict(sorted(ports.items())),
        }
    _write_json(layout.deployment, manifest)
    return {
        "status": "ready",
        "root": str(root),
        "manifest_sha256": _sha256_file(layout.deployment),
        "ports": dict(sorted(ports.items())),
    }


def _load_deployment(root: Path) -> tuple[Layout, dict[str, Any]]:
    root = _validate_root(root)
    layout = _ensure_layout(root)
    manifest = _read_json(layout.deployment)
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("version") != VERSION
        or manifest.get("root") != str(root)
        or type(manifest.get("ports")) is not dict
        or type(manifest.get("urls")) is not dict
        or type(manifest.get("repositories")) is not dict
    ):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "deployment_manifest")
    return layout, manifest


def _contains_canvas_authority(value: object, *, server_id: str) -> bool:
    """Match the production ``Agent.params.mcp`` entry as one authority unit.

    A server id in one component and a tool name in a prompt or another MCP
    entry must never be combined into an apparent authorization relationship.
    """

    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, Mapping):
            params = item.get("params")
            if item.get("component_name") == "Agent" and isinstance(params, Mapping):
                entries = params.get("mcp")
                if isinstance(entries, list):
                    for entry in entries:
                        if not isinstance(entry, Mapping):
                            continue
                        tools = entry.get("tools")
                        if entry.get("mcp_id") == server_id and isinstance(tools, Mapping) and "leave_preview_leave_form" in tools:
                            return True
            pending.extend(item.values())
        elif isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            pending.extend(item)
    return False


def _candidate_ref(values: Iterable[object]) -> str:
    digest = hashlib.sha256()
    digest.update(b"multirag.eim-o5.candidate.v1\x00")
    for value in values:
        digest.update(str(value).encode())
        digest.update(b"\x00")
    return f"candidate-{digest.hexdigest()[:20]}"


async def _query_candidates(manifest: Mapping[str, Any]) -> list[Candidate]:
    """Read existing DB authority without creating or mutating any row."""

    from sqlalchemy import select

    from api.db.db_models import (
        ChannelBinding,
        ChannelSecret,
        ChatChannel,
        ExternalIdentity,
        ExternalIdentityAlias,
        IdentityProviderAccount,
        IdentityProviderChannelLink,
        MCPServer,
        User,
        UserCanvas,
        UserCanvasVersion,
        UserTenant,
        async_session_factory,
    )
    from common.bootstrap import ensure_initialized
    from common.constants import MCPServerType

    ensure_initialized(initialize_resources=False)
    if async_session_factory is None:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "database")
    gateway_url = cast(dict[str, str], manifest["urls"])["gateway"]
    channel_manifest = cast(dict[str, Any], cast(dict[str, Any], manifest["secrets"])["channel"])
    expected_key_ids = channel_manifest.get("key_ids")
    if type(expected_key_ids) is not list or not expected_key_ids or any(type(item) is not str or len(item) != 16 for item in expected_key_ids):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_key_ids")
    available_key_ids = frozenset(cast(list[str], expected_key_ids))
    candidates: list[Candidate] = []
    async with async_session_factory() as session:
        authority_rows = (
            await session.execute(
                select(
                    ChatChannel,
                    ChannelBinding,
                    ChannelSecret,
                    IdentityProviderAccount,
                )
                .join(ChannelBinding, ChannelBinding.channel_id == ChatChannel.id)
                .join(ChannelSecret, ChannelSecret.channel_id == ChatChannel.id)
                .join(IdentityProviderChannelLink, IdentityProviderChannelLink.channel_id == ChatChannel.id)
                .join(IdentityProviderAccount, IdentityProviderAccount.id == IdentityProviderChannelLink.provider_account_id)
                .where(
                    ChatChannel.channel == "feishu",
                    ChatChannel.status == 1,
                    ChannelBinding.enabled.is_(True),
                    ChannelBinding.target_type == "multirag.canvas_agent",
                    IdentityProviderAccount.provider == "feishu",
                    IdentityProviderAccount.tenant_id == ChatChannel.tenant_id,
                    IdentityProviderAccount.identity_health_state == "healthy",
                )
                .order_by(ChatChannel.id, ChannelBinding.id),
            )
        ).all()
        for channel, binding, channel_secret, account in authority_rows:
            if binding.target_revision_id is None:
                continue
            if channel_secret.key_id not in available_key_ids:
                continue
            canvas = await session.scalar(
                select(UserCanvas).where(
                    UserCanvas.id == binding.target_id,
                    UserCanvas.canvas_category == "agent_canvas",
                ),
            )
            latest = await session.scalar(
                select(UserCanvasVersion)
                .where(
                    UserCanvasVersion.user_canvas_id == binding.target_id,
                    UserCanvasVersion.release.is_(True),
                )
                .order_by(UserCanvasVersion.create_time.desc())
                .limit(1),
            )
            if canvas is None or latest is None or latest.id != binding.target_revision_id:
                continue
            servers = list(
                (
                    await session.scalars(
                        select(MCPServer).where(
                            MCPServer.tenant_id == channel.tenant_id,
                            MCPServer.url == gateway_url,
                            MCPServer.server_type == MCPServerType.STREAMABLE_HTTP.value,
                        ),
                    )
                ).all(),
            )
            for server in servers:
                headers = server.headers if isinstance(server.headers, dict) else {}
                if any(str(key).lower() == "authorization" for key in headers):
                    continue
                if not _contains_canvas_authority(latest.dsl, server_id=server.id):
                    continue
                identities = (
                    await session.execute(
                        select(ExternalIdentity)
                        .join(
                            ExternalIdentityAlias,
                            (ExternalIdentityAlias.external_identity_id == ExternalIdentity.id)
                            & (ExternalIdentityAlias.tenant_id == ExternalIdentity.tenant_id)
                            & (ExternalIdentityAlias.provider == ExternalIdentity.provider)
                            & (ExternalIdentityAlias.provider_tenant_key == ExternalIdentity.provider_tenant_key),
                        )
                        .join(
                            UserTenant,
                            (UserTenant.user_id == ExternalIdentity.user_id) & (UserTenant.tenant_id == ExternalIdentity.tenant_id),
                        )
                        .join(User, User.id == ExternalIdentity.user_id)
                        .where(
                            ExternalIdentity.tenant_id == channel.tenant_id,
                            ExternalIdentity.provider == "feishu",
                            ExternalIdentity.provider_tenant_key == account.provider_tenant_key,
                            ExternalIdentity.subject_type == "user_id",
                            ExternalIdentity.state == "active",
                            ExternalIdentity.verified_at.is_not(None),
                            ExternalIdentity.last_seen_at.is_not(None),
                            ExternalIdentityAlias.provider_account_key == account.provider_account_key,
                            ExternalIdentityAlias.verified_at.is_not(None),
                            UserTenant.status == "1",
                            User.is_active.is_(True),
                            User.status == "1",
                        )
                        .order_by(ExternalIdentity.id),
                    )
                ).scalars()
                for identity in identities:
                    values = (
                        channel.tenant_id,
                        identity.user_id,
                        account.provider_tenant_key,
                        account.id,
                        identity.id,
                        identity.identity_revision,
                        channel.id,
                        binding.id,
                        binding.generation,
                        binding.target_id,
                        binding.target_revision_id,
                        server.id,
                    )
                    candidates.append(
                        Candidate(
                            ref=_candidate_ref(values),
                            tenant_id=channel.tenant_id,
                            platform_user_id=identity.user_id,
                            provider_tenant=account.provider_tenant_key,
                            provider_account_id=account.id,
                            external_identity_id=identity.id,
                            identity_revision=identity.identity_revision,
                            channel_id=channel.id,
                            binding_id=binding.id,
                            binding_generation=binding.generation,
                            agent_id=binding.target_id,
                            agent_revision_id=binding.target_revision_id,
                            mcp_server_id=server.id,
                        ),
                    )
    unique = {candidate.ref: candidate for candidate in candidates}
    return [unique[key] for key in sorted(unique)]


def _persist_discovery(layout: Layout, candidates: Sequence[Candidate]) -> dict[str, object]:
    discovery_path = layout.run / "discovery.json"
    document = {
        "schema": f"{SCHEMA}/discovery",
        "version": VERSION,
        "created_at": _now(),
        "candidates": [candidate.sensitive_document() for candidate in candidates],
    }
    _write_json(discovery_path, document)
    if len(candidates) == 1:
        selection = {
            "schema": f"{SCHEMA}/selection",
            "version": VERSION,
            "candidate_ref": candidates[0].ref,
        }
        if layout.selection.exists():
            current = _read_json(layout.selection)
            if current != selection:
                raise OperatorError(ExitCode.AUTHORITY_STALE, "selection_changed")
        else:
            _write_json(layout.selection, selection)
    return {
        "status": "ready" if len(candidates) == 1 else "selection_required",
        "candidate_count": len(candidates),
        "candidates": [candidate.safe_document() for candidate in candidates],
    }


def discover(root: Path, *, _lock: bool = True) -> dict[str, object]:
    layout, manifest = _load_deployment(root)
    if _lock:
        with _operation_lock(layout):
            return discover(root, _lock=False)
    try:
        candidates = asyncio.run(_query_candidates(manifest))
    except OperatorError:
        raise
    except Exception as exc:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "database_authority") from exc
    if not candidates:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "eligible_authority")
    return _persist_discovery(layout, candidates)


def _load_candidates(layout: Layout) -> list[Candidate]:
    document = _read_json(layout.run / "discovery.json")
    if document.get("schema") != f"{SCHEMA}/discovery" or document.get("version") != VERSION:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "discovery")
    raw_candidates = document.get("candidates")
    if type(raw_candidates) is not list or not raw_candidates:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "discovery_candidates")
    try:
        candidates = [Candidate(**raw) for raw in raw_candidates if type(raw) is dict]
    except (TypeError, ValueError) as exc:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "discovery_candidate") from exc
    if len(candidates) != len(raw_candidates) or len({item.ref for item in candidates}) != len(candidates):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "discovery_candidate_set")
    return candidates


def _selected_candidate(layout: Layout) -> Candidate:
    selection = _read_json(layout.selection)
    if selection.get("schema") != f"{SCHEMA}/selection" or selection.get("version") != VERSION:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "selection")
    ref = selection.get("candidate_ref")
    matches = [candidate for candidate in _load_candidates(layout) if candidate.ref == ref]
    if len(matches) != 1:
        raise OperatorError(ExitCode.AUTHORITY_STALE, "selection_ref")
    return matches[0]


def _reveal_config(value: object) -> object:
    """Serialize a typed config for a mode-0600 overlay without redacting it."""

    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, BaseModel):
        return {name: _reveal_config(getattr(value, name)) for name in type(value).model_fields}
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _reveal_config(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_reveal_config(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _build_identity_overlay(
    layout: Layout,
    manifest: Mapping[str, Any],
    *,
    interactions_enabled: bool,
) -> dict[str, object]:
    from common.app_config import get_app_config
    from common.bootstrap import ensure_initialized

    ensure_initialized(initialize_resources=False)
    config = get_app_config()
    # The external overlay is a frozen copy of the complete effective
    # configuration.  config_utils replaces whole top-level sections, so an
    # identity-only document would accidentally discard every other effective
    # section if the process starts without the original local files.
    document = cast(dict[str, object], copy.deepcopy(config.raw))
    identity = cast(dict[str, object], _reveal_config(config.identity))
    urls = cast(dict[str, str], manifest["urls"])
    secrets_manifest = cast(dict[str, Any], manifest["secrets"])
    p3 = cast(dict[str, str], secrets_manifest["p3"])
    interaction = cast(dict[str, str], secrets_manifest["interaction"])
    pki = cast(dict[str, Any], manifest["pki"])
    ca_file = cast(dict[str, str], pki["ca"])["cert_file"]
    interaction_key = Path(interaction["key_file"]).read_text(encoding="ascii").strip()
    identity["mcp_issuer"] = {
        "enabled": True,
        "issuer": urls["issuer"],
        "client_id": "multirag-channel-host",
        "ttl_seconds": 300,
        "clock_skew_seconds": 30,
        "jwks_cache_ttl_seconds": 300,
        "resources": {
            RESOURCE_NAME: {
                "audience": urls["gateway"],
                "registered_scopes": ["leave:read"],
                "allow_provider_identity": True,
            },
        },
        "key_provider": {
            "kind": "file",
            "active_key_id": p3["kid"],
            "private_key_file": p3["private_key_file"],
            "public_key_files": {p3["kid"]: p3["public_key_file"]},
        },
    }
    identity["mcp_delegation"] = {
        "enabled": True,
        "tool_policy_file": str(layout.artifacts / "tool-policies.json"),
        "grant_policy_file": str(layout.artifacts / "mcp-grants.json"),
        "tls_ca_bundle_file": ca_file,
    }
    interactions = cast(dict[str, object], identity.get("mcp_interactions", {}))
    interactions["enabled"] = interactions_enabled
    interactions["payload_encryption_keys"] = [interaction_key]
    identity["mcp_interactions"] = interactions
    document["identity"] = identity
    return document


def _write_overlays(layout: Layout, manifest: Mapping[str, Any]) -> None:
    for stage, enabled in (("a", False), ("b", True)):
        document = _build_identity_overlay(
            layout,
            manifest,
            interactions_enabled=enabled,
        )
        encoded = yaml.safe_dump(document, allow_unicode=True, sort_keys=True).encode()
        _atomic_write(layout.artifacts / f"api-stage-{stage}.yaml", encoded)


def _policy_environment(candidate: Candidate, manifest: Mapping[str, Any]) -> dict[str, str]:
    urls = cast(dict[str, str], manifest["urls"])
    gateway_base = urls["gateway"].removesuffix("/mcp")
    environment = _base_process_env()
    environment.update(
        {
            "OFMCP_GATEWAY_AUTH_ISSUER": urls["issuer"],
            "OFMCP_GATEWAY_AUTH_JWKS_URI": urls["jwks"],
            "OFMCP_GATEWAY_AUTH_RESOURCE_BASE_URL": gateway_base,
            "OFMCP_GATEWAY_AUTH_EXPECTED_AUDIENCE": urls["gateway"],
            "OFMCP_GATEWAY_AUTH_EXPECTED_TENANT_ID": candidate.tenant_id,
            "OFMCP_GATEWAY_AUTH_PROVIDER_IDENTITY_BINDINGS": json.dumps(
                {
                    "leave_applicant": {
                        "any_of": [
                            {
                                "provider": "feishu",
                                "provider_tenant": candidate.provider_tenant,
                                "subject_type": "user_id",
                            },
                        ],
                    },
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
    )
    return environment


def _grant_document(
    candidate: Candidate,
    *,
    audience: str,
    policy_revision: str,
    generation: int,
) -> dict[str, object]:
    document: dict[str, object] = {
        "snapshot_format": 1,
        "policy_revision": policy_revision,
        "credential_generation": generation,
        "bindings": [
            {
                "mcp_server_id": candidate.mcp_server_id,
                "resource_name": RESOURCE_NAME,
                "audience": audience,
            },
        ],
        "grants": [
            {
                "tenant_id": candidate.tenant_id,
                "platform_user_id": candidate.platform_user_id,
                "agent_id": candidate.agent_id,
                "agent_revision_id": candidate.agent_revision_id,
                "resource_name": RESOURCE_NAME,
                "allowed_scopes": ["leave:read"],
            },
        ],
    }
    document["grant_revision"] = _sha256_bytes(_canonical_bytes(document))
    return document


def _next_generation(path: Path) -> int:
    if not path.exists():
        return 1
    document = _read_json(path)
    value = document.get("credential_generation")
    if type(value) is not int or value < 1 or value >= (1 << 63) - 1:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "credential_generation")
    return value + 1


def _grant_source_document(
    candidate: Candidate,
    *,
    audience: str,
    policy_revision: str,
) -> dict[str, object]:
    source = {
        "candidate": candidate.sensitive_document(),
        "audience": audience,
        "policy_revision": policy_revision,
        "resource_name": RESOURCE_NAME,
        "allowed_scopes": ["leave:read"],
    }
    return {
        "schema": f"{SCHEMA}/grant-source",
        "version": VERSION,
        # Store only a digest: the production grant already contains its
        # reviewed identifiers, while Channel/provider binding coordinates do
        # not belong in another plaintext artifact.
        "semantic_sha256": _sha256_bytes(_canonical_bytes(source)),
    }


def _grant_for_publish(
    path: Path,
    candidate: Candidate,
    *,
    audience: str,
    policy_revision: str,
) -> dict[str, object]:
    source_path = path.with_name("mcp-grants.source.json")
    expected_source = _grant_source_document(
        candidate,
        audience=audience,
        policy_revision=policy_revision,
    )
    generation = 1
    existing: dict[str, Any] | None = None
    if path.exists():
        existing = _read_json(path)
        raw_generation = existing.get("credential_generation")
        if type(raw_generation) is not int or raw_generation < 1:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "credential_generation")
        generation = raw_generation
    document = _grant_document(
        candidate,
        audience=audience,
        policy_revision=policy_revision,
        generation=generation,
    )
    existing_source = _read_json(source_path) if source_path.exists() else None
    if existing is None or (existing == document and existing_source == expected_source):
        return document
    return _grant_document(
        candidate,
        audience=audience,
        policy_revision=policy_revision,
        generation=_next_generation(path),
    )


def _postgres_dsn(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> str:
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    userinfo = ""
    if user:
        userinfo = quote(user, safe="")
        if password:
            userinfo += f":{quote(password, safe='')}"
        userinfo += "@"
    return f"postgresql://{userinfo}{rendered_host}:{port}/{quote(database, safe='')}"


def _load_a6_dsn(layout: Layout) -> tuple[Path, str]:
    """Require a user-provisioned A6 database distinct from MultiRAG's DB."""

    from common.app_config import get_app_config
    from common.bootstrap import ensure_initialized

    dsn_path = layout.secrets / "ofmcp-a6-dsn.txt"
    if not dsn_path.exists():
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "a6_dsn_file")
    _assert_regular(dsn_path, mode=0o600)
    dsn = dsn_path.read_text(encoding="utf-8").strip()
    database = _effective_postgres_dbname(dsn)
    ensure_initialized(initialize_resources=False)
    if database == get_app_config().postgresql.dbname:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "a6_database_not_distinct")
    return dsn_path, dsn


def _ensure_a6_database(layout: Layout) -> str:
    import psycopg
    from psycopg import sql

    from common.app_config import get_app_config

    _, target_dsn = _load_a6_dsn(layout)
    target_database = _effective_postgres_dbname(target_dsn)
    try:
        with psycopg.connect(target_dsn, connect_timeout=5):
            return target_database
    except psycopg.Error:
        pass

    config = get_app_config().postgresql
    parsed = urlsplit(target_dsn)
    target_user = unquote(parsed.username or "")
    target_port = parsed.port or 5432
    if parsed.hostname != config.host or target_port != config.port or target_user != config.user:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "a6_database")
    admin_dsn = _postgres_dsn(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.dbname,
    )
    try:
        with psycopg.connect(
            admin_dsn,
            connect_timeout=5,
            autocommit=True,
        ) as connection:
            exists = connection.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (target_database,),
            ).fetchone()
            if exists is None:
                connection.execute(
                    sql.SQL("CREATE DATABASE {}").format(
                        sql.Identifier(target_database),
                    ),
                )
        with psycopg.connect(target_dsn, connect_timeout=5):
            return target_database
    except psycopg.Error as exc:
        raise OperatorError(
            ExitCode.PREREQUISITE_MISSING,
            "a6_database",
        ) from exc


def _effective_postgres_dbname(dsn: str) -> str:
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql", "postgresql+psycopg"} or not parsed.hostname:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "a6_dsn")
    database = unquote(parsed.path.lstrip("/"))
    query_dbnames = [unquote(value) for name, value in parse_qsl(parsed.query, keep_blank_values=True) if name == "dbname"]
    if len(query_dbnames) > 1:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "a6_database_ambiguous")
    if query_dbnames:
        database = query_dbnames[0]
    if not database or "/" in database:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "a6_database")
    return database


def _write_public_key_manifest(layout: Layout, manifest: Mapping[str, Any]) -> Path:
    secrets_manifest = cast(dict[str, Any], manifest["secrets"])
    p3 = cast(dict[str, str], secrets_manifest["p3"])
    path = layout.artifacts / "jwks-public-keys.json"
    document = {
        "format": 1,
        "active_key_id": p3["kid"],
        "jwks_cache_ttl_seconds": 300,
        "public_key_files": {p3["kid"]: p3["public_key_file"]},
    }
    _write_json(path, document)
    return path


def prepare(
    root: Path,
    *,
    apply: bool,
    runner: CommandRunner | None = None,
    _lock: bool = True,
    _allow_running: bool = False,
) -> dict[str, object]:
    layout, manifest = _load_deployment(root)
    if _lock:
        with _operation_lock(layout):
            return prepare(
                root,
                apply=apply,
                runner=runner,
                _lock=False,
                _allow_running=_allow_running,
            )
    _load_a6_dsn(layout)
    repositories = cast(dict[str, str], manifest["repositories"])
    ofmcp_repo = Path(repositories["ofmcp"])
    # Refresh DB authority immediately before creating reviewed artifacts.
    live_candidates = asyncio.run(_query_candidates(manifest))
    _persist_discovery(layout, live_candidates)
    candidate = _selected_candidate(layout)
    if not apply:
        return {
            "status": "dry_run_ready",
            "candidate_ref": candidate.ref,
            "actions": ["policy_snapshot", "grant_snapshot", "stage_overlays"],
        }

    if not _allow_running:
        _require_quiescent_runtime(layout, manifest)

    command_runner = runner or SubprocessCommandRunner()
    next_policy = layout.artifacts / ".tool-policies.next.json"
    if next_policy.exists() or next_policy.is_symlink():
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "stale_policy_temp")
    _safe_command(
        command_runner,
        [
            "uv",
            "run",
            "ofmcp",
            "contract",
            "policy-snapshot",
            "--profile",
            "secure",
            "--out",
            str(next_policy),
        ],
        cwd=ofmcp_repo,
        env=_policy_environment(candidate, manifest),
    )
    os.chmod(next_policy, 0o600)
    from api.identity.mcp_delegation.policy import (
        load_grant_policy_snapshot,
        load_tool_policy_snapshot,
    )

    tool_policy = load_tool_policy_snapshot(next_policy)
    leave_policy = tool_policy.tools.get("leave_preview_leave_form")
    if (
        leave_policy is None
        or leave_policy.required_scopes != frozenset({"leave:read"})
        or leave_policy.effect != "prepare"
        or leave_policy.replay_mode != "reusable"
        or leave_policy.provider_identity is None
        or not any(
            coordinate.provider == "feishu" and coordinate.provider_tenant == candidate.provider_tenant and coordinate.subject_type == "user_id" for coordinate in leave_policy.provider_identity.any_of
        )
    ):
        next_policy.unlink(missing_ok=True)
        raise OperatorError(ExitCode.AUTHORITY_STALE, "leave_policy")
    policy_path = layout.artifacts / "tool-policies.json"
    grant_path = layout.artifacts / "mcp-grants.json"
    grant_document = _grant_for_publish(
        grant_path,
        candidate,
        audience=cast(dict[str, str], manifest["urls"])["gateway"],
        policy_revision=tool_policy.policy_revision,
    )
    next_grant = layout.artifacts / ".mcp-grants.next.json"
    next_grant_source = layout.artifacts / ".mcp-grants.source.next.json"
    _write_json(next_grant, grant_document)
    _write_json(
        next_grant_source,
        _grant_source_document(
            candidate,
            audience=cast(dict[str, str], manifest["urls"])["gateway"],
            policy_revision=tool_policy.policy_revision,
        ),
    )
    grant_policy = load_grant_policy_snapshot(next_grant, tool_policy=tool_policy)
    os.replace(next_policy, policy_path)
    os.replace(next_grant, grant_path)
    os.replace(
        next_grant_source,
        layout.artifacts / "mcp-grants.source.json",
    )
    os.chmod(policy_path, 0o600)
    os.chmod(grant_path, 0o600)
    # Re-read the published artifacts through production validators.
    reloaded_policy = load_tool_policy_snapshot(policy_path)
    reloaded_grant = load_grant_policy_snapshot(grant_path, tool_policy=reloaded_policy)
    if reloaded_grant.grant_revision != grant_policy.grant_revision:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "grant_publish")
    _write_public_key_manifest(layout, manifest)
    _write_overlays(layout, manifest)
    return {
        "status": "prepared",
        "candidate_ref": candidate.ref,
        "credential_generation": reloaded_grant.credential_generation,
        "policy_revision": reloaded_policy.policy_revision,
        "grant_revision": reloaded_grant.grant_revision,
    }


def _repo_state(repo: Path) -> dict[str, object]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=10,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=20,
    )
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--"],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if head.returncode != 0 or status.returncode != 0 or diff.returncode != 0:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "git_state")
    head_sha = head.stdout.decode("ascii", errors="strict").strip()
    if len(head_sha) != 40 or any(character not in "0123456789abcdef" for character in head_sha):
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "git_head")
    digest = hashlib.sha256()
    digest.update(b"multirag.secure-leave.repo-state.v1\x00")
    digest.update(head_sha.encode("ascii"))
    digest.update(b"\x00status\x00")
    digest.update(status.stdout)
    digest.update(b"\x00diff\x00")
    digest.update(diff.stdout)
    for entry in status.stdout.split(b"\x00"):
        if not entry.startswith(b"?? "):
            continue
        try:
            relative = Path(entry[3:].decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise OperatorError(ExitCode.PREREQUISITE_MISSING, "git_untracked_path") from exc
        path = (repo / relative).resolve(strict=False)
        if repo.resolve() not in path.parents or path.is_symlink() or not path.is_file():
            raise OperatorError(ExitCode.PREREQUISITE_MISSING, "git_untracked_file")
        digest.update(b"\x00untracked\x00")
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\x00")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(128 * 1024), b""):
                digest.update(chunk)
    return {
        "head_sha": head_sha,
        "dirty": bool(status.stdout),
        "worktree_sha256": digest.hexdigest(),
    }


def _repository_dirty_paths(repo: Path) -> frozenset[str]:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=20,
    )
    if status.returncode != 0:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "git_status")
    paths: set[str] = set()
    for entry in status.stdout.split(b"\x00"):
        if not entry:
            continue
        if len(entry) < 4 or entry[2:3] != b" ":
            raise OperatorError(ExitCode.PREREQUISITE_MISSING, "git_status_shape")
        try:
            paths.add(entry[3:].decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise OperatorError(ExitCode.PREREQUISITE_MISSING, "git_status_path") from exc
    return frozenset(paths)


def _require_immutable_implementation_repositories(
    manifest: Mapping[str, Any],
) -> None:
    repositories = cast(dict[str, str], manifest["repositories"])
    multirag_dirty = _repository_dirty_paths(Path(repositories["multirag"]))
    ofmcp_dirty = _repository_dirty_paths(Path(repositories["ofmcp"]))
    # The user's existing machine-local base configuration is an explicit
    # protected input. It may remain modified, but no source, test, overlay, or
    # deployment artifact may be uncommitted when a live proof begins.
    if multirag_dirty - {"configs/service_conf.yaml"} or ofmcp_dirty:
        raise OperatorError(
            ExitCode.LIVE_EVIDENCE_PENDING,
            "implementation_repository_dirty",
        )


def _repo_sha(repo: Path) -> str:
    return cast(str, _repo_state(repo)["head_sha"])


def _argv_hash(argv: Sequence[str]) -> str:
    return _sha256_bytes("\x00".join(argv).encode())


def _exact_options(argv: Sequence[str], *, required: frozenset[str]) -> dict[str, str] | None:
    if len(argv) != len(required) * 2:
        return None
    options: dict[str, str] = {}
    for index in range(0, len(argv), 2):
        name = argv[index]
        value = argv[index + 1]
        if name not in required or name in options or not value:
            return None
        options[name] = value
    return options if set(options) == required else None


def _classify_process(argv: Sequence[str]) -> str | None:
    if not argv:
        return None
    executable = Path(argv[0]).name
    if executable.startswith("python") and tuple(argv[1:]) == ("-m", "api.multirag_server"):
        return "api"
    if executable.startswith("python") and tuple(argv[1:]) == ("-m", "api.channels.supervisor"):
        return "supervisor"
    if executable.startswith("python") and tuple(argv[1:3]) == ("-m", "api.identity.jwks_publisher"):
        options = _exact_options(
            argv[3:],
            required=frozenset(
                {
                    "--host",
                    "--port",
                    "--cert-file",
                    "--key-file",
                    "--public-key-manifest",
                },
            ),
        )
        if options is not None and options["--host"] == "127.0.0.1":
            return "jwks"
        return None
    if executable == "ofmcp" and tuple(argv[1:2]) == ("serve",):
        options = _exact_options(
            argv[2:],
            required=frozenset(
                {
                    "--profile",
                    "--host",
                    "--port",
                    "--tls-cert-file",
                    "--tls-key-file",
                },
            ),
        )
        if options is not None and options["--profile"] == "secure" and options["--host"] == "127.0.0.1":
            return "gateway"
        return None
    if executable == "ofmcp-ecology-mock" and tuple(argv[1:2]) == ("serve",):
        options = _exact_options(
            argv[2:],
            required=frozenset({"--private-key", "--host", "--port"}),
        )
        if options is not None and options["--host"] == "127.0.0.1":
            return "mock"
    return None


def _runtime_argv_matches_spec(argv: Sequence[str], spec: ProcessSpec) -> bool:
    """Match the post-exec argv, including every security-relevant value."""

    if _classify_process(argv) != spec.name:
        return False
    if not spec.argv:
        # Inventory-only specs are read-only.  Any destructive takeover uses
        # a fully populated spec built from the deployment manifest.
        return True
    expected = tuple(spec.argv)
    if expected[:2] == ("uv", "run"):
        expected = expected[2:]
    elif spec.name == "api":
        expected = ("python", "-m", "api.multirag_server")
    elif spec.name == "supervisor":
        expected = ("python", "-m", "api.channels.supervisor")
    if not expected or not argv:
        return False
    actual_executable = Path(argv[0]).name
    expected_executable = Path(expected[0]).name
    executable_matches = actual_executable.startswith("python") if expected_executable == "python" else actual_executable == expected_executable
    return executable_matches and tuple(argv[1:]) == expected[1:]


def _process_cwd(process: psutil.Process) -> Path | None:
    try:
        return Path(process.cwd()).resolve()
    except (psutil.AccessDenied, psutil.NoSuchProcess, FileNotFoundError):
        return None


def _process_cmdline(process: psutil.Process) -> tuple[str, ...]:
    try:
        return tuple(process.cmdline())
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return ()


def _listener(port: int) -> psutil.Process | None:
    try:
        connections = psutil.net_connections(kind="inet")
    except psutil.AccessDenied as exc:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "port_inventory") from exc
    owners = {item.pid for item in connections if item.status == psutil.CONN_LISTEN and item.laddr and item.laddr.port == port and item.pid is not None}
    if not owners:
        return None
    if len(owners) != 1:
        raise OperatorError(ExitCode.PORT_IN_USE, f"port={port} pid=multiple")
    return psutil.Process(owners.pop())


def _same_repo_allowed(process: psutil.Process, spec: ProcessSpec) -> bool:
    return _process_cwd(process) == spec.cwd.resolve() and _runtime_argv_matches_spec(
        _process_cmdline(process),
        spec,
    )


def _matching_processes(spec: ProcessSpec) -> tuple[psutil.Process, ...]:
    """Return every live exact allowlisted process for one repository/spec."""

    matches: list[psutil.Process] = []
    for process in psutil.process_iter():
        if process.pid in {os.getpid(), os.getppid()}:
            continue
        if _same_repo_allowed(process, spec):
            matches.append(process)
    return tuple(matches)


def _process_group_members(process_group: int) -> tuple[psutil.Process, ...]:
    members: list[psutil.Process] = []
    for candidate in psutil.process_iter():
        try:
            if os.getpgid(candidate.pid) == process_group:
                members.append(candidate)
        except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
            continue
    return tuple(members)


def _wait_for_group_exit(process_group: int, *, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _process_group_members(process_group):
            return True
        time.sleep(0.1)
    return not _process_group_members(process_group)


def _stop_confirmed_process(process: psutil.Process, *, process_group: int | None = None) -> None:
    pid = process.pid
    if pid in {os.getpid(), os.getppid()}:
        raise OperatorError(ExitCode.PROCESS_NOT_OWNED, "self_process")
    group_owned = process_group == pid
    if group_owned:
        try:
            group_owned = os.getpgid(pid) == pid
        except ProcessLookupError:
            # The leader can exit between the provenance check and this
            # lookup.  Its session children can still own the confirmed PGID,
            # so only an empty group is safe to treat as already stopped.
            if not _process_group_members(pid):
                return
            group_owned = True
    leader_exited = False
    try:
        # The supervisor receives the first signal directly and gets a full
        # grace period to drain its workers.  Only a still-live owned process
        # group is escalated; a blind group-first kill would strand runtime
        # leases and lose callback work.
        process.terminate()
        try:
            process.wait(timeout=10)
            leader_exited = True
        except psutil.TimeoutExpired:
            pass
        if leader_exited and (not group_owned or _wait_for_group_exit(pid, seconds=1)):
            return
    except psutil.NoSuchProcess:
        leader_exited = True
        if not group_owned or _wait_for_group_exit(pid, seconds=1):
            return
    try:
        if group_owned:
            os.killpg(pid, signal.SIGTERM)
            if _wait_for_group_exit(pid, seconds=3):
                return
            os.killpg(pid, signal.SIGKILL)
        else:
            process.kill()
        if group_owned:
            if not _wait_for_group_exit(pid, seconds=5):
                raise OperatorError(ExitCode.PROCESS_FAILED, f"pid={pid} stop_timeout")
        else:
            process.wait(timeout=5)
    except (ProcessLookupError, psutil.NoSuchProcess):
        return
    except psutil.TimeoutExpired as exc:
        raise OperatorError(ExitCode.PROCESS_FAILED, f"pid={pid} stop_timeout") from exc


def _terminate_spawned_process(process: subprocess.Popen[bytes]) -> None:
    expected_group = process.pid
    try:
        process_group = os.getpgid(process.pid)
    except ProcessLookupError:
        if not _process_group_members(expected_group):
            return
        process_group = expected_group
    try:
        if process_group == process.pid:
            os.killpg(process_group, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        if process_group != process.pid or _wait_for_group_exit(process_group, seconds=1):
            return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        if process_group == process.pid:
            os.killpg(process_group, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=3)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        return


def _stabilize_spawned_process(
    process: subprocess.Popen[bytes],
    spec: ProcessSpec,
    *,
    seconds: float = 10.0,
) -> tuple[psutil.Process, tuple[str, ...]]:
    deadline = time.monotonic() + seconds
    previous: tuple[str, ...] | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise OperatorError(ExitCode.PROCESS_FAILED, f"process={spec.name} exited")
        try:
            observed = psutil.Process(process.pid)
            argv = _process_cmdline(observed)
            valid = _process_cwd(observed) == spec.cwd.resolve() and _runtime_argv_matches_spec(
                argv,
                spec,
            )
        except psutil.NoSuchProcess:
            valid = False
            argv = ()
        if valid and argv == previous:
            return observed, argv
        previous = argv if valid else None
        time.sleep(0.05)
    raise OperatorError(ExitCode.PROCESS_FAILED, f"process={spec.name} exec_unstable")


def _base_process_env() -> dict[str, str]:
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "TMPDIR",
        "USER",
        "UV_CACHE_DIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
    return {name: value for name, value in os.environ.items() if name in allowed}


def _api_environment(
    layout: Layout,
    manifest: Mapping[str, Any],
    *,
    stage: str,
) -> dict[str, str]:
    environment = _base_process_env()
    environment.update(
        {name: value for name, value in os.environ.items() if name.startswith("MULTIRAG_") and name != "MULTIRAG_CONFIG_OVERLAY_FILE" and not name.startswith("MULTIRAG_IDENTITY__")},
    )
    environment.update(_read_env_file(layout.secrets / "channel/api.env"))
    environment["MULTIRAG_SECRETS_DIR"] = str(layout.secrets / "channel")
    environment["MULTIRAG_MULTIRAG__HOST"] = "127.0.0.1"
    environment["MULTIRAG_MULTIRAG__HTTP_PORT"] = str(
        cast(dict[str, int], manifest["ports"])["api"],
    )
    environment["MULTIRAG_CONFIG_OVERLAY_FILE"] = str(layout.artifacts / f"api-stage-{stage}.yaml")
    return environment


def _supervisor_environment(layout: Layout) -> dict[str, str]:
    environment = _base_process_env()
    environment.update(_read_env_file(layout.secrets / "channel/supervisor.env"))
    environment["MULTIRAG_SECRETS_DIR"] = str(layout.secrets / "channel")
    environment.pop("MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY", None)
    return environment


def _gateway_environment(
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
) -> dict[str, str]:
    dsn_path, _ = _load_a6_dsn(layout)
    environment = _base_process_env()
    environment.update(_policy_environment(candidate, manifest))
    environment["OFMCP_GATEWAY_AUTH_JWKS_CA_BUNDLE_FILE"] = cast(dict[str, Any], manifest["pki"])["ca"]["cert_file"]
    environment["OFMCP_GATEWAY_SECURITY_STORE_DSN_FILE"] = str(dsn_path)
    ofmcp_manifest = _read_json(layout.root / "ofmcp-deployment.json")
    ofmcp_secrets = cast(dict[str, Any], ofmcp_manifest["secrets"])
    request_state = Path(ofmcp_secrets["request_state_key_ring"]["path"])
    fingerprint = Path(ofmcp_secrets["a6_fingerprint_key_ring"]["path"])
    identity = Path(ofmcp_secrets["a6_identity_key"]["path"])
    _assert_regular(request_state, mode=0o600)
    _assert_regular(fingerprint, mode=0o600)
    _assert_regular(identity, mode=0o600)
    environment["OFMCP_REQUEST_STATE_KEY"] = request_state.read_text(encoding="ascii").strip()
    environment["OFMCP_GATEWAY_SECURITY_FINGERPRINT_KEY_FILE"] = str(fingerprint)
    environment["OFMCP_GATEWAY_SECURITY_IDENTITY_KEY_FILE"] = str(identity)
    secrets_manifest = cast(dict[str, Any], manifest["secrets"])
    mock = cast(dict[str, str], secrets_manifest["ecology_mock"])
    environment.update(
        {
            "OFMCP_LEAVE_OA_BASE_URL": cast(dict[str, str], manifest["urls"])["mock"],
            "OFMCP_LEAVE_APPID": "ofmcp-local-demo",
            "OFMCP_LEAVE_SECRIT": "ofmcp-local-demo-secret",
            "OFMCP_LEAVE_SPK": Path(mock["public_spk_file"]).read_text(encoding="ascii").strip(),
        },
    )
    return environment


def _build_specs(
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
    *,
    api_stage: str,
) -> dict[str, ProcessSpec]:
    repositories = cast(dict[str, str], manifest["repositories"])
    ofmcp_repo = Path(repositories["ofmcp"]).resolve()
    ports = cast(dict[str, int], manifest["ports"])
    pki = cast(dict[str, Any], manifest["pki"])
    leaves = cast(dict[str, Any], pki["leaves"])
    secrets_manifest = cast(dict[str, Any], manifest["secrets"])
    mock = cast(dict[str, str], secrets_manifest["ecology_mock"])
    public_manifest = layout.artifacts / "jwks-public-keys.json"
    _assert_regular(public_manifest, mode=0o600)
    return {
        "jwks": ProcessSpec(
            name="jwks",
            argv=(
                "uv",
                "run",
                "python",
                "-m",
                "api.identity.jwks_publisher",
                "--host",
                "127.0.0.1",
                "--port",
                str(ports["jwks"]),
                "--cert-file",
                leaves["issuer"]["cert_file"],
                "--key-file",
                leaves["issuer"]["key_file"],
                "--public-key-manifest",
                str(public_manifest),
            ),
            cwd=REPOSITORY_ROOT,
            env=_base_process_env(),
            port=ports["jwks"],
        ),
        "api": ProcessSpec(
            name="api",
            argv=("sh", str(REPOSITORY_ROOT / "scripts/run_api.example.sh")),
            cwd=REPOSITORY_ROOT,
            env=_api_environment(layout, manifest, stage=api_stage),
            port=ports["api"],
        ),
        "mock": ProcessSpec(
            name="mock",
            argv=(
                "uv",
                "run",
                "ofmcp-ecology-mock",
                "serve",
                "--private-key",
                mock["private_key_file"],
                "--host",
                "127.0.0.1",
                "--port",
                str(ports["mock"]),
            ),
            cwd=ofmcp_repo,
            env=_base_process_env(),
            port=ports["mock"],
        ),
        "gateway": ProcessSpec(
            name="gateway",
            argv=(
                "uv",
                "run",
                "ofmcp",
                "serve",
                "--profile",
                "secure",
                "--host",
                "127.0.0.1",
                "--port",
                str(ports["gateway"]),
                "--tls-cert-file",
                leaves["gateway"]["cert_file"],
                "--tls-key-file",
                leaves["gateway"]["key_file"],
            ),
            cwd=ofmcp_repo,
            env=_gateway_environment(layout, manifest, candidate),
            port=ports["gateway"],
        ),
        "supervisor": ProcessSpec(
            name="supervisor",
            argv=(
                "sh",
                str(REPOSITORY_ROOT / "scripts/run_channel_supervisor.example.sh"),
            ),
            cwd=REPOSITORY_ROOT,
            env=_supervisor_environment(layout),
        ),
    }


def _inventory_specs(manifest: Mapping[str, Any]) -> dict[str, ProcessSpec]:
    repositories = cast(dict[str, str], manifest["repositories"])
    ofmcp_repo = Path(repositories["ofmcp"]).resolve()
    ports = cast(dict[str, int], manifest["ports"])
    return {
        "api": ProcessSpec("api", (), REPOSITORY_ROOT, port=ports["api"]),
        "gateway": ProcessSpec("gateway", (), ofmcp_repo, port=ports["gateway"]),
        "jwks": ProcessSpec("jwks", (), REPOSITORY_ROOT, port=ports["jwks"]),
        "mock": ProcessSpec("mock", (), ofmcp_repo, port=ports["mock"]),
        "supervisor": ProcessSpec("supervisor", (), REPOSITORY_ROOT),
    }


def _preflight_inventory(layout: Layout, manifest: Mapping[str, Any]) -> dict[str, object]:
    specs = _inventory_specs(manifest)
    manager = ProcessManager(layout)
    snapshot = manager.snapshot(specs)
    drifted = [name for name, state in snapshot.items() if isinstance(state, dict) and state.get("state") == "ownership_lost"]
    if drifted:
        raise OperatorError(ExitCode.PROCESS_NOT_OWNED, f"record={','.join(sorted(drifted))}")
    observed: dict[str, object] = {}
    for name, spec in specs.items():
        if spec.port is None:
            continue
        matching = _matching_processes(spec)
        if len(matching) > 1:
            raise OperatorError(
                ExitCode.PROCESS_NOT_OWNED,
                f"process={name} duplicate",
            )
        owner = _listener(spec.port)
        if owner is None:
            observed[name] = (
                {
                    "port": spec.port,
                    "state": "same_repo",
                    "pid": matching[0].pid,
                    "phase": "starting",
                }
                if matching
                else {"port": spec.port, "state": "free"}
            )
            continue
        cwd = _process_cwd(owner)
        if not _same_repo_allowed(owner, spec):
            raise OperatorError(
                ExitCode.PORT_IN_USE,
                f"port={spec.port} pid={owner.pid} cwd={cwd or 'unavailable'}",
            )
        if matching and matching[0].pid != owner.pid:
            raise OperatorError(
                ExitCode.PROCESS_NOT_OWNED,
                f"process={name} listener_mismatch",
            )
        observed[name] = {"port": spec.port, "state": "same_repo", "pid": owner.pid}
    supervisor_spec = specs["supervisor"]
    supervisor_count = 0
    for process in psutil.process_iter():
        if process.pid in {os.getpid(), os.getppid()}:
            continue
        argv = _process_cmdline(process)
        if _classify_process(argv) != "supervisor":
            continue
        supervisor_count += 1
        cwd = _process_cwd(process)
        if not _same_repo_allowed(process, supervisor_spec):
            raise OperatorError(
                ExitCode.PROCESS_NOT_OWNED,
                f"process=supervisor pid={process.pid} cwd={cwd or 'unavailable'}",
            )
    if supervisor_count > 1:
        raise OperatorError(ExitCode.PROCESS_NOT_OWNED, "process=supervisor duplicate")
    observed["supervisor"] = {
        "state": "same_repo" if supervisor_count else "free",
    }
    return observed


class ProcessManager:
    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        self.record_path = layout.run / "processes.json"

    def _load_records(self) -> dict[str, ProcessRecord]:
        if not self.record_path.exists():
            return {}
        document = _read_json(self.record_path)
        if document.get("schema") != f"{SCHEMA}/processes" or document.get("version") != VERSION:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "process_records")
        raw = document.get("processes")
        if type(raw) is not dict:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "process_record_shape")
        try:
            return {name: ProcessRecord(**record) for name, record in raw.items()}
        except (TypeError, ValueError) as exc:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "process_record") from exc

    def _save_records(self, records: Mapping[str, ProcessRecord]) -> None:
        _write_json(
            self.record_path,
            {
                "schema": f"{SCHEMA}/processes",
                "version": VERSION,
                "updated_at": _now(),
                "processes": {name: asdict(record) for name, record in sorted(records.items())},
            },
        )

    def _record_lifecycle(self, event: str, record: ProcessRecord) -> None:
        if event not in {"started", "stopped"}:
            raise OperatorError(ExitCode.INTERNAL_ERROR, "process_event")
        _append_json_line(
            self.layout.run / "process-events.ndjson",
            {
                "event": event,
                "at": _now(),
                "record": asdict(record),
            },
        )

    def _api_overlay_binding(self, spec: ProcessSpec) -> tuple[str | None, str | None]:
        if spec.name != "api":
            return None, None
        raw_overlay = spec.env.get("MULTIRAG_CONFIG_OVERLAY_FILE")
        if raw_overlay is None:
            # Inventory-only specs intentionally omit environment details so
            # they can disable either stage during fail-closed recovery.
            return None, None
        overlay = Path(raw_overlay)
        expected = {
            (self.layout.artifacts / "api-stage-a.yaml").resolve(): "a",
            (self.layout.artifacts / "api-stage-b.yaml").resolve(): "b",
        }
        try:
            stage = expected[overlay.resolve(strict=True)]
        except (KeyError, OSError) as exc:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "api_overlay_path") from exc
        _assert_regular(overlay, mode=0o600)
        return stage, _sha256_file(overlay)

    def prune_definitely_dead_records(self) -> tuple[str, ...]:
        """Remove only records whose PID is proven absent by the kernel."""

        records = self._load_records()
        removable: list[str] = []
        for name, record in records.items():
            if psutil.pid_exists(record.pid):
                continue
            if _process_group_members(record.process_group):
                raise OperatorError(
                    ExitCode.PROCESS_NOT_OWNED,
                    f"process={name} orphan_group={record.process_group}",
                )
            removable.append(name)
        removed = tuple(sorted(removable))
        if removed:
            for name in removed:
                del records[name]
            self._save_records(records)
        return removed

    def _verify_record(self, record: ProcessRecord, spec: ProcessSpec) -> psutil.Process:
        try:
            process = psutil.Process(record.pid)
            create_time = process.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            raise OperatorError(ExitCode.PROCESS_NOT_OWNED, f"pid={record.pid} unavailable") from exc
        argv = _process_cmdline(process)
        try:
            process_group = os.getpgid(record.pid)
        except ProcessLookupError as exc:
            raise OperatorError(
                ExitCode.PROCESS_NOT_OWNED,
                f"pid={record.pid} unavailable",
            ) from exc
        expected_stage, expected_overlay_sha256 = self._api_overlay_binding(spec)
        binding_mismatch = (spec.name == "api" and expected_stage is not None and (record.api_stage != expected_stage or record.overlay_sha256 != expected_overlay_sha256)) or (
            spec.name != "api" and (record.api_stage is not None or record.overlay_sha256 is not None)
        )
        if (
            abs(create_time - record.create_time) > 0.01
            or process_group != record.process_group
            or process_group != record.pid
            or _process_cwd(process) != spec.cwd.resolve()
            or not _runtime_argv_matches_spec(argv, spec)
            or _argv_hash(argv) != record.argv_sha256
            or binding_mismatch
        ):
            raise OperatorError(ExitCode.PROCESS_NOT_OWNED, f"pid={record.pid} provenance_mismatch")
        return process

    def _take_over_matching(self, spec: ProcessSpec) -> None:
        if spec.port is None:
            for process in psutil.process_iter():
                if process.pid in {os.getpid(), os.getppid()}:
                    continue
                if not _same_repo_allowed(process, spec):
                    continue
                try:
                    process_group = os.getpgid(process.pid)
                except ProcessLookupError:
                    continue
                if spec.name == "supervisor" and process_group != process.pid:
                    raise OperatorError(
                        ExitCode.PROCESS_NOT_OWNED,
                        f"pid={process.pid} supervisor_group_unowned",
                    )
                _stop_confirmed_process(
                    process,
                    process_group=process.pid if process_group == process.pid else None,
                )
            return
        matching = _matching_processes(spec)
        if len(matching) > 1:
            raise OperatorError(
                ExitCode.PROCESS_NOT_OWNED,
                f"process={spec.name} duplicate",
            )
        if matching:
            process = matching[0]
            try:
                matching_process_group: int | None = os.getpgid(process.pid)
            except ProcessLookupError:
                matching_process_group = None
            _stop_confirmed_process(
                process,
                process_group=(process.pid if matching_process_group == process.pid else None),
            )
        owner = _listener(spec.port)
        if owner is None:
            return
        cwd = _process_cwd(owner)
        if not _same_repo_allowed(owner, spec):
            raise OperatorError(
                ExitCode.PORT_IN_USE,
                f"port={spec.port} pid={owner.pid} cwd={cwd or 'unavailable'}",
            )
        try:
            process_group = os.getpgid(owner.pid)
        except ProcessLookupError:
            return
        _stop_confirmed_process(
            owner,
            process_group=owner.pid if process_group == owner.pid else None,
        )

    def start(self, spec: ProcessSpec) -> ProcessRecord:
        api_stage, overlay_sha256 = self._api_overlay_binding(spec)
        existing_records = self._load_records()
        existing = existing_records.get(spec.name)
        if existing is not None:
            try:
                psutil.Process(existing.pid)
            except psutil.NoSuchProcess:
                if _process_group_members(existing.process_group):
                    raise OperatorError(
                        ExitCode.PROCESS_NOT_OWNED,
                        f"process={spec.name} orphan_group={existing.process_group}",
                    )
                del existing_records[spec.name]
                self._save_records(existing_records)
                self._take_over_matching(spec)
            except psutil.AccessDenied as exc:
                raise OperatorError(
                    ExitCode.PROCESS_NOT_OWNED,
                    f"pid={existing.pid} inaccessible",
                ) from exc
            else:
                self.stop(spec)
        else:
            self._take_over_matching(spec)
        log_path = self.layout.logs / f"{spec.name}.log"
        if log_path.is_symlink() or (log_path.exists() and not log_path.is_file()):
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "log_path")
        descriptor = os.open(
            log_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_NOFOLLOW,
            0o600,
        )
        os.chmod(log_path, 0o600)
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                list(spec.argv),
                cwd=spec.cwd,
                env=dict(spec.env),
                stdin=subprocess.DEVNULL,
                stdout=descriptor,
                stderr=descriptor,
                start_new_session=True,
            )
        finally:
            os.close(descriptor)
        if process is None:
            raise OperatorError(ExitCode.PROCESS_FAILED, f"process={spec.name} spawn")
        try:
            ps_process, stable_argv = _stabilize_spawned_process(process, spec)
            process_group = os.getpgid(process.pid)
            if process_group != process.pid:
                raise OperatorError(ExitCode.PROCESS_FAILED, f"process={spec.name} process_group")
            repository = _repo_state(spec.cwd)
            record = ProcessRecord(
                name=spec.name,
                pid=process.pid,
                create_time=ps_process.create_time(),
                cwd=str(spec.cwd.resolve()),
                argv_sha256=_argv_hash(stable_argv),
                process_group=process_group,
                repo_head_sha=cast(str, repository["head_sha"]),
                repo_dirty=cast(bool, repository["dirty"]),
                repo_state_sha256=cast(str, repository["worktree_sha256"]),
                api_stage=api_stage,
                overlay_sha256=overlay_sha256,
            )
            records = self._load_records()
            records[spec.name] = record
            # Append the evidence event before publishing ownership. If either
            # durable write fails, the spawned process is terminated and no
            # ownership record can survive without its corresponding event.
            self._record_lifecycle("started", record)
            self._save_records(records)
            return record
        except BaseException:
            _terminate_spawned_process(process)
            raise

    def stop(self, spec: ProcessSpec) -> None:
        records = self._load_records()
        record = records.get(spec.name)
        if record is None:
            # A known same-repo process may predate our ownership file.  The
            # user's approved takeover still requires the same strict proof.
            self._take_over_matching(spec)
            return
        if not psutil.pid_exists(record.pid):
            if _process_group_members(record.process_group):
                raise OperatorError(
                    ExitCode.PROCESS_NOT_OWNED,
                    f"process={spec.name} orphan_group={record.process_group}",
                )
            del records[spec.name]
            self._save_records(records)
            self._take_over_matching(spec)
            return
        process = self._verify_record(record, spec)
        _stop_confirmed_process(process, process_group=record.process_group)
        self._record_lifecycle("stopped", record)
        del records[spec.name]
        self._save_records(records)

    def snapshot(self, specs: Mapping[str, ProcessSpec]) -> dict[str, object]:
        records = self._load_records()
        result: dict[str, object] = {}
        for name, spec in specs.items():
            record = records.get(name)
            if record is None:
                result[name] = {"state": "stopped"}
                continue
            try:
                self._verify_record(record, spec)
            except OperatorError:
                result[name] = {"state": "ownership_lost", "pid": record.pid}
            else:
                result[name] = {
                    "state": "running",
                    "pid": record.pid,
                    "repo_head_sha": record.repo_head_sha,
                    "repo_dirty": record.repo_dirty,
                    "repo_state_sha256": record.repo_state_sha256,
                }
        return result


def _fetch_json(
    url: str,
    *,
    ca_file: Path | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 3.0,
) -> dict[str, Any]:
    context = ssl.create_default_context(cafile=str(ca_file)) if ca_file is not None else None
    request = urllib.request.Request(url, headers=dict(headers or {}))
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(1 << 20)
            if response.status != 200 or len(body) >= 1 << 20:
                raise ValueError
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise OperatorError(ExitCode.PROCESS_FAILED, "http_check") from exc
    try:
        value = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OperatorError(ExitCode.PROCESS_FAILED, "http_json") from exc
    if type(value) is not dict:
        raise OperatorError(ExitCode.PROCESS_FAILED, "http_shape")
    return cast(dict[str, Any], value)


def _wait_for(check: Callable[[], object], *, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            check()
            return
        except OperatorError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def _verify_managed_record(
    layout: Layout,
    spec: ProcessSpec,
) -> None:
    manager = ProcessManager(layout)
    record = manager._load_records().get(spec.name)
    if record is None:
        raise OperatorError(ExitCode.PROCESS_FAILED, f"managed_process={spec.name}")
    manager._verify_record(record, spec)


def _runtime_runner_matches_group(
    runner_id: object,
    candidate: Candidate,
    *,
    process_group: int,
) -> bool:
    if type(runner_id) is not str:
        return False
    _, separator, raw_pid = runner_id.rpartition("-")
    if not separator or not raw_pid.isascii() or not raw_pid.isdigit():
        return False
    pid = int(raw_pid)
    try:
        process = psutil.Process(pid)
        argv = _process_cmdline(process)
        expected = (
            "-m",
            "api.channels.worker",
            "--channel",
            "feishu",
            "--binding-id",
            candidate.binding_id,
            "--binding-generation",
            str(candidate.binding_generation),
        )
        return os.getpgid(pid) == process_group and _process_cwd(process) == REPOSITORY_ROOT and bool(argv) and Path(argv[0]).name.startswith("python") and tuple(argv[1:]) == expected
    except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
        return False


async def _runtime_is_connected(
    candidate: Candidate,
    *,
    not_before: datetime | None = None,
    process_group: int | None = None,
) -> bool:
    from sqlalchemy import select

    from api.db.db_models import ChannelRuntimeStatus, async_session_factory
    from common.bootstrap import ensure_initialized

    ensure_initialized(initialize_resources=False)
    if async_session_factory is None:
        return False
    freshness_cutoff = datetime.now(UTC) - timedelta(seconds=45)
    if not_before is not None and not_before > freshness_cutoff:
        freshness_cutoff = not_before
    async with async_session_factory() as session:
        runtime = await session.scalar(
            select(ChannelRuntimeStatus).where(
                ChannelRuntimeStatus.binding_id == candidate.binding_id,
                ChannelRuntimeStatus.observed_generation == candidate.binding_generation,
                ChannelRuntimeStatus.state == "connected",
                ChannelRuntimeStatus.runner_id.is_not(None),
                ChannelRuntimeStatus.heartbeat_at >= freshness_cutoff,
            ),
        )
        return runtime is not None and (
            process_group is None
            or _runtime_runner_matches_group(
                runtime.runner_id,
                candidate,
                process_group=process_group,
            )
        )


def _wait_for_runtime(
    candidate: Candidate,
    *,
    not_before: datetime | None = None,
    process_group: int | None = None,
    seconds: float = 45.0,
) -> None:
    def check() -> None:
        if not asyncio.run(
            _runtime_is_connected(
                candidate,
                not_before=not_before,
                process_group=process_group,
            ),
        ):
            raise OperatorError(ExitCode.PROCESS_FAILED, "channel_runtime")

    _wait_for(check, seconds=seconds)


def _migrate(layout: Layout, manifest: Mapping[str, Any], runner: CommandRunner) -> None:
    repositories = cast(dict[str, str], manifest["repositories"])
    ofmcp_repo = Path(repositories["ofmcp"])
    multirag_env = dict(os.environ)
    multirag_env.pop("MULTIRAG_CONFIG_OVERLAY_FILE", None)
    _safe_command(runner, ["uv", "run", "alembic", "upgrade", "head"], cwd=REPOSITORY_ROOT, env=multirag_env)
    _ensure_a6_database(layout)
    _, dsn = _load_a6_dsn(layout)
    a6_env = _base_process_env()
    a6_env["OFMCP_GATEWAY_SECURITY_STORE_DSN"] = dsn
    _safe_command(runner, ["uv", "run", "ofmcp-a6-migrate"], cwd=ofmcp_repo, env=a6_env)


def _require_quiescent_runtime(
    layout: Layout,
    manifest: Mapping[str, Any],
) -> None:
    observed = _preflight_inventory(layout, manifest)
    recorded = ProcessManager(layout).snapshot(_inventory_specs(manifest))
    active = sorted(
        {name for name, item in observed.items() if isinstance(item, Mapping) and item.get("state") == "same_repo"}
        | {name for name, item in recorded.items() if isinstance(item, Mapping) and item.get("state") == "running"},
    )
    if active:
        raise OperatorError(
            ExitCode.PROCESS_FAILED,
            f"runtime_active={','.join(active)}",
        )


def _quiesce_managed_runtime(
    layout: Layout,
    manifest: Mapping[str, Any],
) -> tuple[str, ...]:
    """Disable the old producer, drain its consumer, then stop dependencies."""

    observed = _preflight_inventory(layout, manifest)
    manager = ProcessManager(layout)
    inventory_specs = _inventory_specs(manifest)
    stopped: list[str] = []
    manager.stop(inventory_specs["api"])
    stopped.append("api")
    supervisor_running = isinstance(observed.get("supervisor"), Mapping) and cast(Mapping[str, object], observed["supervisor"]).get("state") == "same_repo"
    stage_a: dict[str, ProcessSpec] | None = None
    if supervisor_running:
        candidate = _selected_candidate(layout)
        stage_a = _build_specs(layout, manifest, candidate, api_stage="a")
        urls = cast(dict[str, str], manifest["urls"])
        try:
            manager.start(stage_a["api"])
            _wait_for(lambda: _fetch_json(f"{urls['api']}/api/v1/system/ping"))
        except OperatorError as exc:
            raise OperatorError(ExitCode.PROCESS_FAILED, "quiesce=api_stage_a") from exc
        # A supervisor failure is a hard gate. Stage A and all dependencies
        # remain available so durable callback/resume work can be recovered.
        try:
            manager.stop(stage_a["supervisor"])
        except OperatorError as exc:
            raise OperatorError(ExitCode.PROCESS_FAILED, "quiesce=supervisor") from exc
        stopped.append("supervisor")
    for name in ("gateway", "mock", "jwks"):
        manager.stop(inventory_specs[name])
        stopped.append(name)
    if stage_a is not None:
        manager.stop(stage_a["api"])
        stopped.append("api_stage_a")
    return tuple(stopped)


def _rollback_failed_up(
    manager: ProcessManager,
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
    *,
    started: set[str],
) -> None:
    stage_a = _build_specs(layout, manifest, candidate, api_stage="a")
    inventory = _inventory_specs(manifest)
    urls = cast(dict[str, str], manifest["urls"])

    # Producer closure is the rollback gate.  If its ownership cannot be
    # proven, do not tear down the consumer or backend underneath it.
    actions: list[str] = []
    if "api" in started:
        # A failure may happen before or after the Stage-B swap.  The exact
        # ownership record still binds PID/create-time/cwd/argv/process-group;
        # the inventory spec intentionally omits only the A/B overlay binding
        # so either producer state can be closed before rollback proceeds.
        manager.stop(inventory["api"])
        actions.append("api_disable_producer")
    errors: list[str] = []
    stage_a_started = False
    if "supervisor" in started:
        try:
            manager.start(stage_a["api"])
            stage_a_started = True
            _wait_for(lambda: _fetch_json(f"{urls['api']}/api/v1/system/ping"))
        except OperatorError as exc:
            raise OperatorError(ExitCode.PROCESS_FAILED, "rollback=api_stage_a") from exc
        try:
            manager.stop(stage_a["supervisor"])
            actions.append("supervisor")
        except OperatorError as exc:
            # Keep Stage A and all dependencies alive for operator recovery.
            raise OperatorError(ExitCode.PROCESS_FAILED, "rollback=supervisor") from exc
    for name in ("gateway", "mock", "jwks"):
        if name in started:
            try:
                manager.stop(stage_a[name])
                actions.append(name)
            except OperatorError:
                errors.append(name)
    if stage_a_started:
        try:
            manager.stop(stage_a["api"])
            actions.append("api")
        except OperatorError:
            errors.append("api")
    if errors:
        raise OperatorError(ExitCode.PROCESS_FAILED, f"rollback={','.join(errors)}")
    expected = tuple(name for name in ROLLBACK_ORDER if name in actions)
    if tuple(actions) != expected:
        raise OperatorError(ExitCode.INTERNAL_ERROR, "rollback_order")


def up(root: Path, *, runner: CommandRunner | None = None) -> dict[str, object]:
    layout, _ = _load_deployment(root)
    with _operation_lock(layout):
        return _up_unlocked(root, runner=runner)


def _up_unlocked(root: Path, *, runner: CommandRunner | None = None) -> dict[str, object]:
    layout, manifest = _load_deployment(root)
    command_runner = runner or SubprocessCommandRunner()
    active_stage = "inventory"
    _record_stage(layout, "inventory", "begin")
    # ``up`` holds the operation lock here.  Prune only kernel-confirmed dead
    # PIDs before snapshotting; reused/inaccessible live PIDs remain a hard
    # provenance failure.
    ProcessManager(layout).prune_definitely_dead_records()
    _preflight_inventory(layout, manifest)
    _quiesce_managed_runtime(layout, manifest)
    _record_stage(layout, "inventory", "complete")
    active_stage = "migrations"
    _record_stage(layout, "migrations", "begin")
    _migrate(layout, manifest, command_runner)
    _record_stage(layout, "migrations", "complete")
    active_stage = "authority_prepare"
    _record_stage(layout, "authority_prepare", "begin")
    prepare(
        root,
        apply=True,
        runner=command_runner,
        _lock=False,
        _allow_running=True,
    )
    _record_stage(layout, "authority_prepare", "complete")
    candidate = _selected_candidate(layout)
    specs = _build_specs(layout, manifest, candidate, api_stage="a")
    manager = ProcessManager(layout)
    urls = cast(dict[str, str], manifest["urls"])
    ca_file = Path(cast(dict[str, Any], manifest["pki"])["ca"]["cert_file"])
    started: set[str] = set()
    try:
        active_stage = "jwks"
        _record_stage(layout, "jwks", "begin")
        manager.start(specs["jwks"])
        started.add("jwks")
        _wait_for(lambda: _fetch_json(f"{urls['issuer']}/livez", ca_file=ca_file))
        _record_stage(layout, "jwks", "complete")
        active_stage = "api_stage_a"
        _record_stage(layout, "api_stage_a", "begin")
        manager.start(specs["api"])
        started.add("api")
        _wait_for(lambda: _fetch_json(f"{urls['api']}/api/v1/system/ping"))
        _record_stage(layout, "api_stage_a", "complete")
        active_stage = "mock"
        _record_stage(layout, "mock", "begin")
        manager.start(specs["mock"])
        started.add("mock")
        _wait_for(lambda: _fetch_json(f"{urls['mock']}/healthz"))
        _record_stage(layout, "mock", "complete")
        active_stage = "gateway"
        _record_stage(layout, "gateway", "begin")
        manager.start(specs["gateway"])
        started.add("gateway")
        _wait_for(lambda: _fetch_json(f"{urls['gateway'].removesuffix('/mcp')}/health/ready", ca_file=ca_file), seconds=45)
        _validate_gateway_before_consumer(layout, manifest, candidate)
        _record_stage(layout, "gateway", "complete")
        active_stage = "supervisor_consumer"
        _record_stage(layout, "supervisor_consumer", "begin")
        supervisor_record = manager.start(specs["supervisor"])
        started.add("supervisor")
        _wait_for_runtime(
            candidate,
            not_before=datetime.fromtimestamp(supervisor_record.create_time, UTC),
            process_group=supervisor_record.process_group,
        )
        _validate_interaction_capability(layout, manifest, candidate)
        _record_stage(layout, "supervisor_consumer", "complete")

        # Producer gate is enabled only after the new consumer is connected.
        active_stage = "api_stage_b"
        _record_stage(layout, "api_stage_b", "begin")
        manager.stop(specs["api"])
        specs = _build_specs(layout, manifest, candidate, api_stage="b")
        manager.start(specs["api"])
        _wait_for(lambda: _fetch_json(f"{urls['api']}/api/v1/system/ping"))
        _record_stage(layout, "api_stage_b", "complete")
        active_stage = "online_doctor"
        _record_stage(layout, "online_doctor", "begin")
        online = doctor(root, online=True)
        if online["status"] != "ready":
            raise OperatorError(ExitCode.PROCESS_FAILED, "online_doctor")
        _record_stage(layout, "online_doctor", "complete")
    except BaseException:
        try:
            _record_stage(layout, active_stage, "failed")
        except BaseException:
            # Rollback is the fail-closed priority even if progress logging is
            # unavailable.  The original exception is re-raised afterwards.
            pass
        _rollback_failed_up(
            manager,
            layout,
            manifest,
            candidate,
            started=started,
        )
        raise
    return {
        "status": "running",
        "stage": "b",
        "candidate_ref": candidate.ref,
        "processes": manager.snapshot(specs),
    }


def status(root: Path) -> dict[str, object]:
    layout, manifest = _load_deployment(root)
    candidate = _selected_candidate(layout)
    specs = _build_specs(layout, manifest, candidate, api_stage="b")
    return {
        "status": "observed",
        "processes": ProcessManager(layout).snapshot(specs),
    }


def down(root: Path) -> dict[str, object]:
    layout, _ = _load_deployment(root)
    with _operation_lock(layout):
        return _down_unlocked(root)


def _down_unlocked(root: Path) -> dict[str, object]:
    layout, manifest = _load_deployment(root)
    manager = ProcessManager(layout)
    # The emergency safety action depends only on the immutable deployment
    # manifest and the exact allowlisted API process.  Do it before reading
    # selection/policy/grant/key material: drift in those files must never
    # leave a Stage-B producer running.
    manager.stop(_inventory_specs(manifest)["api"])
    try:
        candidate = _selected_candidate(layout)
        stage_a = _build_specs(layout, manifest, candidate, api_stage="a")
    except OperatorError as exc:
        # Producer is now off.  Preserve the supervisor and dependencies for
        # operator recovery instead of attempting an unsafe partial teardown.
        raise OperatorError(
            ExitCode.PROCESS_FAILED,
            "down=stage_a_config",
        ) from exc
    # Briefly re-open API with the consumer-only config, then drain/stop the
    # Channel supervisor before the remaining services.
    urls = cast(dict[str, str], manifest["urls"])
    try:
        manager.start(stage_a["api"])
        _wait_for(lambda: _fetch_json(f"{urls['api']}/api/v1/system/ping"))
    except OperatorError as exc:
        raise OperatorError(ExitCode.PROCESS_FAILED, "down=api_stage_a") from exc
    stopped: list[str] = []
    try:
        manager.stop(stage_a["supervisor"])
        stopped.append("supervisor")
    except OperatorError as exc:
        # Producer is disabled and Stage A remains live.  Do not remove the
        # dependencies from underneath an undrained Channel consumer.
        raise OperatorError(ExitCode.PROCESS_FAILED, "down=supervisor") from exc
    errors: list[str] = []
    for name in ("gateway", "mock", "jwks"):
        try:
            manager.stop(stage_a[name])
            stopped.append(name)
        except OperatorError:
            errors.append(name)
    try:
        manager.stop(stage_a["api"])
        stopped.append("api")
    except OperatorError:
        errors.append("api")
    if errors:
        raise OperatorError(ExitCode.PROCESS_FAILED, f"down={','.join(errors)}")
    return {"status": "stopped", "order": stopped, "data_preserved": True}


def _restart_waiting_runtime(
    root: Path,
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
) -> WaitingRestartOutcome:
    """Restart only API and Channel consumer while one form awaits input."""

    manager = ProcessManager(layout)
    stage_b = _build_specs(layout, manifest, candidate, api_stage="b")
    stage_a = _build_specs(layout, manifest, candidate, api_stage="a")
    urls = cast(dict[str, str], manifest["urls"])
    runtime_before = _runtime_repository_evidence(
        layout,
        manifest,
        candidate,
    )
    mock_before = _mock_run_boundary(layout, manifest)

    # Fail closed first: disable new interaction production, then drain and
    # replace the consumer while durable awaiting-input state stays in DB.
    manager.stop(stage_b["api"])
    try:
        manager.start(stage_a["api"])
        _wait_for(lambda: _fetch_json(f"{urls['api']}/api/v1/system/ping"))
    except OperatorError as exc:
        raise OperatorError(
            ExitCode.PROCESS_FAILED,
            "restart_waiting=api_stage_a",
        ) from exc

    try:
        manager.stop(stage_a["supervisor"])
        supervisor_record = manager.start(stage_a["supervisor"])
        _wait_for_runtime(
            candidate,
            not_before=datetime.fromtimestamp(
                supervisor_record.create_time,
                UTC,
            ),
            process_group=supervisor_record.process_group,
        )
        _validate_interaction_capability(layout, manifest, candidate)
    except OperatorError as exc:
        # Stage A intentionally remains up and the producer remains disabled.
        raise OperatorError(
            ExitCode.PROCESS_FAILED,
            "restart_waiting=supervisor",
        ) from exc

    try:
        manager.stop(stage_a["api"])
        manager.start(stage_b["api"])
        _wait_for(lambda: _fetch_json(f"{urls['api']}/api/v1/system/ping"))
        result = doctor(root, online=True)
        if result["status"] != "ready":
            raise OperatorError(
                ExitCode.PROCESS_FAILED,
                "online_doctor",
            )
        runtime_after = _runtime_repository_evidence(
            layout,
            manifest,
            candidate,
        )
        mock_after = _mock_run_boundary(layout, manifest)
        if any(runtime_after[name] != runtime_before[name] for name in ("jwks", "mock", "gateway")) or mock_after != mock_before:
            raise OperatorError(
                ExitCode.PROCESS_NOT_OWNED,
                "restart_waiting=nonrestartable_changed",
            )
    except BaseException as exc:
        # If Stage B cannot be proven healthy, make a best-effort return to
        # Stage A.  Failure to recover is reported separately and never
        # silently leaves an unverified producer running.
        try:
            manager.stop(_inventory_specs(manifest)["api"])
            manager.start(stage_a["api"])
            _wait_for(
                lambda: _fetch_json(
                    f"{urls['api']}/api/v1/system/ping",
                ),
            )
        except BaseException as recovery_exc:
            raise OperatorError(
                ExitCode.PROCESS_FAILED,
                "restart_waiting=recovery",
            ) from recovery_exc
        if not isinstance(exc, OperatorError):
            raise
        raise OperatorError(
            ExitCode.PROCESS_FAILED,
            "restart_waiting=api_stage_b",
        ) from exc

    return WaitingRestartOutcome(
        response={
            "status": "awaiting_feishu",
            "prompt": FIXED_LIVE_PROMPT,
            "instruction": ("The awaiting-input API and Channel consumer restart succeeded; submit the already delivered seven-field form, then run this probe again."),
        },
        runtime_before=runtime_before,
        runtime_after=runtime_after,
        mock_before=mock_before,
        mock_after=mock_after,
    )


def _reconcile_failed_waiting_restart(
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
) -> None:
    """Recover an interrupted wait restart to a proven producer-off state."""

    manager = ProcessManager(layout)
    inventory = _inventory_specs(manifest)
    stage_a = _build_specs(layout, manifest, candidate, api_stage="a")
    urls = cast(dict[str, str], manifest["urls"])
    try:
        # Inventory specs deliberately accept either recorded API stage. Stop
        # first so no unverified producer can survive reconciliation.
        manager.stop(inventory["api"])
        manager.start(stage_a["api"])
        _wait_for(lambda: _fetch_json(f"{urls['api']}/api/v1/system/ping"))
        manager.stop(inventory["supervisor"])
        supervisor_record = manager.start(stage_a["supervisor"])
        _wait_for_runtime(
            candidate,
            not_before=datetime.fromtimestamp(
                supervisor_record.create_time,
                UTC,
            ),
            process_group=supervisor_record.process_group,
        )
        _validate_interaction_capability(layout, manifest, candidate)
    except BaseException as exc:
        # Stage A was the only API start attempted. Preserve it if available;
        # otherwise the API remains stopped. Both outcomes fail closed.
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        raise OperatorError(
            ExitCode.PROCESS_FAILED,
            "waiting_restart_reconciliation",
        ) from exc


def _archive_abandoned_live_probe(
    layout: Layout,
    state: Mapping[str, object],
    *,
    reason: str,
) -> None:
    archive = layout.run / (f"live-probe-abandoned-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}.json")
    _write_json(
        archive,
        {
            "schema": f"{SCHEMA}/abandoned-live-probe",
            "version": VERSION,
            "reason": reason,
            "previous_started_at": state.get("started_at"),
            "candidate_ref": state.get("candidate_ref"),
        },
    )


async def _build_probe_principal(candidate: Candidate) -> object:
    from sqlalchemy import select

    from api.db.db_models import (
        ExternalIdentity,
        IdentityProviderAccount,
        IdentityProviderChannelLink,
        UserTenant,
        async_session_factory,
    )
    from api.identity.contracts import (
        ExternalIdentityRecord,
        IdentityResolutionResult,
        IdentityResolutionStatus,
        UserMembershipRecord,
    )
    from api.identity.principal import (
        AuthenticationContext,
        AuthenticationSource,
        IdentityAssurance,
        VerifiedProviderIdentity,
        build_principal_from_resolved_identity,
    )
    from common.bootstrap import ensure_initialized

    ensure_initialized(initialize_resources=False)
    if async_session_factory is None:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "database")
    async with async_session_factory() as session:
        identity = await session.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.id == candidate.external_identity_id,
                ExternalIdentity.tenant_id == candidate.tenant_id,
                ExternalIdentity.user_id == candidate.platform_user_id,
                ExternalIdentity.state == "active",
            ),
        )
        membership = await session.scalar(
            select(UserTenant).where(
                UserTenant.tenant_id == candidate.tenant_id,
                UserTenant.user_id == candidate.platform_user_id,
                UserTenant.status == "1",
            ),
        )
        account = await session.scalar(
            select(IdentityProviderAccount)
            .join(
                IdentityProviderChannelLink,
                IdentityProviderChannelLink.provider_account_id == IdentityProviderAccount.id,
            )
            .where(
                IdentityProviderChannelLink.channel_id == candidate.channel_id,
                IdentityProviderAccount.tenant_id == candidate.tenant_id,
                IdentityProviderAccount.provider_tenant_key == candidate.provider_tenant,
                IdentityProviderAccount.identity_health_state == "healthy",
            ),
        )
        if identity is None or membership is None or account is None or identity.verified_at is None:
            raise OperatorError(ExitCode.AUTHORITY_STALE, "probe_identity")
        attributes = tuple(sorted((str(key), value) for key, value in identity.attributes.items()))
        identity_record = ExternalIdentityRecord(
            id=identity.id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            provider=identity.provider,
            provider_tenant_key=identity.provider_tenant_key,
            subject_type=identity.subject_type,
            subject_value=identity.subject_value,
            state=identity.state,
            verified_at=identity.verified_at,
            last_seen_at=identity.last_seen_at,
            identity_revision=identity.identity_revision,
            attributes=attributes,
        )
        now = datetime.now(UTC)
        return build_principal_from_resolved_identity(
            result=IdentityResolutionResult(
                status=IdentityResolutionStatus.RESOLVED,
                identity=identity_record,
                membership=UserMembershipRecord(
                    user_id=membership.user_id,
                    tenant_id=membership.tenant_id,
                    role=membership.role,
                ),
            ),
            authentication=AuthenticationContext(
                source=AuthenticationSource.ENTERPRISE_IDENTITY,
                assurance=IdentityAssurance.DIRECTORY_VERIFIED,
                validated_at=now,
                assurance_verified_at=identity.verified_at,
                provider=identity.provider,
                external_identity_id=identity.id,
            ),
            provider_identity=VerifiedProviderIdentity(
                platform_user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                provider=identity.provider,
                provider_tenant=identity.provider_tenant_key,
                provider_account_id=account.id,
                subject_type=identity.subject_type,
                subject=identity.subject_value,
                verified_at=identity.verified_at,
            ),
        )


async def _authenticated_tools_list(
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
) -> tuple[str, ...]:
    from sqlalchemy import select

    from api.db.db_models import MCPServer, async_session_factory
    from api.identity.mcp_delegation.runtime import (
        get_mcp_delegation_service,
        reset_mcp_delegation_service,
    )
    from api.identity.mcp_issuer.runtime import reset_mcp_token_issuer
    from api.identity.principal import Principal
    from api.identity.run_context import RunContext
    from common.app_config import reset_app_config
    from common.bootstrap import ensure_initialized
    from common.mcp_tool_call_conn import (
        _BearerLeaseAuth,
        _create_delegated_http_client,
    )
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    principal = cast(Principal, await _build_probe_principal(candidate))
    ensure_initialized(initialize_resources=False)
    if async_session_factory is None:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "database")
    async with async_session_factory() as session:
        server = await session.scalar(
            select(MCPServer).where(
                MCPServer.id == candidate.mcp_server_id,
                MCPServer.tenant_id == candidate.tenant_id,
            ),
        )
    if server is None:
        raise OperatorError(ExitCode.AUTHORITY_STALE, "mcp_server")

    overlay = str(layout.artifacts / "api-stage-a.yaml")
    previous_overlay = os.environ.get("MULTIRAG_CONFIG_OVERLAY_FILE")
    os.environ["MULTIRAG_CONFIG_OVERLAY_FILE"] = overlay
    reset_mcp_delegation_service()
    reset_mcp_token_issuer()
    reset_app_config()
    try:
        service = get_mcp_delegation_service()
        provider = service.bind(
            mcp_server=server,
            run_context=RunContext(
                tenant_id=candidate.tenant_id,
                principal=principal,
                agent_id=candidate.agent_id,
                agent_revision_id=candidate.agent_revision_id,
            ),
        )
        if provider is None or not provider.is_authorized("leave_preview_leave_form") or provider.is_authorized("leave_create_leave_draft") or provider.is_authorized("leave_submit_leave"):
            raise OperatorError(ExitCode.AUTHORITY_STALE, "delegation_visibility")
        credential = provider.credential_for("leave_preview_leave_form")
        if credential.resource_name != RESOURCE_NAME or credential.effect != "prepare" or credential.replay_mode != "reusable":
            raise OperatorError(ExitCode.AUTHORITY_STALE, "delegation_credential")
        http_client = _create_delegated_http_client(
            headers=cast(dict[str, str], server.headers or {}),
            auth=_BearerLeaseAuth(credential.bearer),
            tls_ssl_context=provider.tls_ssl_context,
        )
        async with http_client:
            transport = streamable_http_client(str(server.url), http_client=http_client)
            async with Client(transport, mode="auto") as client:
                response = await client.list_tools()
    finally:
        reset_mcp_delegation_service()
        reset_mcp_token_issuer()
        reset_app_config()
        if previous_overlay is None:
            os.environ.pop("MULTIRAG_CONFIG_OVERLAY_FILE", None)
        else:
            os.environ["MULTIRAG_CONFIG_OVERLAY_FILE"] = previous_overlay

    names = tuple(sorted(tool.name for tool in response.tools))
    if "leave_preview_leave_form" not in names or any(name in names for name in {"leave_create_leave_draft", "leave_submit_leave"}):
        raise OperatorError(ExitCode.AUTHORITY_STALE, "tool_visibility")
    return names


def _validate_gateway_before_consumer(
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
) -> None:
    urls = cast(dict[str, str], manifest["urls"])
    ca_file = Path(cast(dict[str, Any], manifest["pki"])["ca"]["cert_file"])
    published = _fetch_json(urls["jwks"], ca_file=ca_file)
    canonical = _fetch_json(f"{urls['api']}/.well-known/jwks.json")
    if _canonical_bytes(published) != _canonical_bytes(canonical):
        raise OperatorError(ExitCode.AUTHORITY_STALE, "jwks_mismatch")
    gateway_base = urls["gateway"].removesuffix("/mcp")
    prm = _fetch_json(
        f"{gateway_base}/.well-known/oauth-protected-resource/mcp",
        ca_file=ca_file,
    )
    if prm.get("resource") != urls["gateway"]:
        raise OperatorError(ExitCode.AUTHORITY_STALE, "gateway_prm")
    asyncio.run(_authenticated_tools_list(layout, manifest, candidate))


def _validate_interaction_capability(
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
) -> None:
    urls = cast(dict[str, str], manifest["urls"])
    token = _read_env_file(layout.secrets / "channel/api.env")[CHANNEL_TOKEN_ENV]
    payload = _fetch_json(
        f"{urls['api']}/api/v1/internal/channel-bindings/{quote(candidate.binding_id, safe='')}/execution-capabilities",
        headers={"Authorization": f"Bearer {token}"},
    )
    if payload.get("interaction_delivery") is not True:
        raise OperatorError(ExitCode.AUTHORITY_STALE, "interaction_capability")


def _check_result(check_id: str, operation: Callable[[], object]) -> Check:
    try:
        operation()
    except OperatorError as exc:
        return Check(check_id=check_id, status="fail", detail=exc.code.value)
    except Exception:
        return Check(check_id=check_id, status="fail", detail=ExitCode.INTERNAL_ERROR.value)
    return Check(check_id=check_id, status="pass", detail="ok")


def _validate_deployment_keys(
    layout: Layout,
    manifest: Mapping[str, Any],
) -> None:
    secrets_manifest = cast(dict[str, Any], manifest["secrets"])
    channel = cast(dict[str, str], secrets_manifest["channel"])
    p3 = cast(dict[str, str], secrets_manifest["p3"])
    mock = cast(dict[str, str], secrets_manifest["ecology_mock"])
    api_values = _read_env_file(Path(channel["api_env_file"]))
    supervisor_values = _read_env_file(Path(channel["supervisor_env_file"]))
    _validate_channel_api_env(api_values)
    _channel_key_ring(api_values[CHANNEL_KEY_ENV])
    if set(supervisor_values) != {
        CHANNEL_TOKEN_ENV,
        "MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL",
    } or not secrets.compare_digest(
        api_values[CHANNEL_TOKEN_ENV],
        supervisor_values[CHANNEL_TOKEN_ENV],
    ):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_secret_pair")
    _validate_p3_pair(
        Path(p3["private_key_file"]),
        Path(p3["public_key_file"]),
    )
    _validate_mock_pair(
        Path(mock["private_key_file"]),
        Path(mock["public_spk_file"]),
    )
    _key_fingerprint_evidence(layout, manifest)


def doctor(root: Path, *, online: bool) -> dict[str, object]:
    layout, manifest = _load_deployment(root)
    candidate = _selected_candidate(layout)
    checks: list[Check] = []
    pki = cast(dict[str, Any], manifest["pki"])
    paths = {
        "ca_key": layout.pki / "ca-key.pem",
        "ca_cert": layout.pki / "ca.pem",
        "issuer_key": layout.pki / "issuer-key.pem",
        "issuer_cert": layout.pki / "issuer-cert.pem",
        "gateway_key": layout.pki / "gateway-key.pem",
        "gateway_cert": layout.pki / "gateway-cert.pem",
    }
    checks.append(_check_result("offline.layout.permissions", lambda: _ensure_layout(layout.root)))
    checks.append(_check_result("offline.pki.chain", lambda: _validate_pki(paths)))
    checks.append(
        _check_result(
            "offline.secrets.keypairs_permissions",
            lambda: _validate_deployment_keys(layout, manifest),
        ),
    )
    checks.append(_check_result("offline.a6.distinct_database", lambda: _load_a6_dsn(layout)))
    checks.append(_check_result("offline.migrations.at_head", lambda: _validate_migration_heads(layout)))
    checks.append(
        _check_result(
            "offline.a6.durability_triggers",
            lambda: _validate_a6_durability(layout),
        ),
    )
    try:
        prunable_rows = _a6_prunable_replay_rows(layout)
    except OperatorError as exc:
        checks.append(
            Check(
                "offline.a6.replay_prunable_rows",
                "fail",
                exc.code.value,
            ),
        )
    else:
        checks.append(
            Check(
                "offline.a6.replay_prunable_rows",
                "pass",
                f"rows={prunable_rows}",
            ),
        )

    def validate_artifacts() -> None:
        from api.identity.mcp_delegation.policy import (
            load_grant_policy_snapshot,
            load_tool_policy_snapshot,
        )

        policy = load_tool_policy_snapshot(layout.artifacts / "tool-policies.json")
        grant = load_grant_policy_snapshot(layout.artifacts / "mcp-grants.json", tool_policy=policy)
        key = (
            candidate.tenant_id,
            candidate.platform_user_id,
            candidate.agent_id,
            candidate.agent_revision_id,
            RESOURCE_NAME,
        )
        if grant.grants.get(key) is None or grant.grants[key].allowed_scopes != frozenset({"leave:read"}):
            raise OperatorError(ExitCode.AUTHORITY_STALE, "grant")

    checks.append(_check_result("offline.authority.policy_grant", validate_artifacts))

    def validate_current_authority() -> None:
        matches = [item for item in asyncio.run(_query_candidates(manifest)) if item == candidate]
        if len(matches) != 1:
            raise OperatorError(ExitCode.AUTHORITY_STALE, "current_authority")

    checks.append(
        _check_result(
            "offline.authority.current_channel_identity_canvas_binding",
            validate_current_authority,
        ),
    )
    specs = _build_specs(layout, manifest, candidate, api_stage="b")

    def validate_ports() -> None:
        for name, spec in specs.items():
            if spec.port is None:
                continue
            owner = _listener(spec.port)
            if owner is not None and not _same_repo_allowed(owner, spec):
                cwd = _process_cwd(owner)
                raise OperatorError(ExitCode.PORT_IN_USE, f"port={spec.port} pid={owner.pid} cwd={cwd or 'unavailable'}")

    checks.append(_check_result("offline.process.port_ownership", validate_ports))
    if online:
        urls = cast(dict[str, str], manifest["urls"])
        ca_file = Path(cast(dict[str, str], pki["ca"])["cert_file"])
        checks.append(_check_result("online.api.ping", lambda: _fetch_json(f"{urls['api']}/api/v1/system/ping")))

        checks.append(
            _check_result(
                "online.process.api_stage_b",
                lambda: _verify_managed_record(layout, specs["api"]),
            ),
        )

        def validate_jwks() -> None:
            public = _fetch_json(urls["jwks"], ca_file=ca_file)
            api = _fetch_json(f"{urls['api']}/.well-known/jwks.json")
            if _canonical_bytes(public) != _canonical_bytes(api):
                raise OperatorError(ExitCode.AUTHORITY_STALE, "jwks_mismatch")

        checks.append(_check_result("online.jwks.byte_equivalence", validate_jwks))
        gateway_base = urls["gateway"].removesuffix("/mcp")
        checks.append(_check_result("online.gateway.ready", lambda: _fetch_json(f"{gateway_base}/health/ready", ca_file=ca_file)))
        checks.append(_check_result("online.gateway.live", lambda: _fetch_json(f"{gateway_base}/health/live", ca_file=ca_file)))
        checks.append(
            _check_result(
                "online.gateway.prm",
                lambda: _fetch_json(f"{gateway_base}/.well-known/oauth-protected-resource/mcp", ca_file=ca_file),
            ),
        )

        def validate_mock() -> None:
            payload = _fetch_json(f"{urls['mock']}/healthz")
            calls = payload.get("calls", {})
            if payload.get("mode") != "mock" or payload.get("write_enabled") is not False or not isinstance(calls, dict) or calls.get("doCreateRequest_blocked", 0) != 0:
                raise OperatorError(ExitCode.ARTIFACT_INVALID, "mock_write_boundary")

        checks.append(_check_result("online.mock.write_disabled", validate_mock))

        def validate_runtime() -> None:
            manager = ProcessManager(layout)
            record = manager._load_records().get("supervisor")
            if record is None:
                raise OperatorError(ExitCode.PROCESS_FAILED, "channel_supervisor")
            manager._verify_record(record, specs["supervisor"])
            if not asyncio.run(
                _runtime_is_connected(
                    candidate,
                    not_before=datetime.fromtimestamp(record.create_time, UTC),
                    process_group=record.process_group,
                ),
            ):
                raise OperatorError(ExitCode.PROCESS_FAILED, "channel_runtime")

        checks.append(_check_result("online.channel.current_generation", validate_runtime))

        checks.append(
            _check_result(
                "online.channel.interaction_capability",
                lambda: _validate_interaction_capability(layout, manifest, candidate),
            ),
        )
        checks.append(
            _check_result(
                "online.process.unique_supervisor",
                lambda: _preflight_inventory(layout, manifest),
            ),
        )
        checks.append(
            _check_result(
                "online.a6.audit_ledger",
                lambda: _a6_audit_high_water(layout),
            ),
        )
        checks.append(
            _check_result(
                "online.gateway.authenticated_tools_list",
                lambda: asyncio.run(_authenticated_tools_list(layout, manifest, candidate)),
            ),
        )
    failed = [check for check in checks if check.status != "pass"]
    return {
        "status": "ready" if not failed else "failed",
        "online": online,
        "checks": [asdict(check) for check in checks],
    }


def _valid_live_delivery_attempts(
    *,
    terminal_delivery_attempt: int,
    resume_attempt: int,
    callback_attempt: int,
) -> bool:
    # One presentation row is delivered first as the native form and then as
    # the strict terminal card.  Resume and callback claims each happen once.
    return terminal_delivery_attempt == 2 and resume_attempt == 1 and callback_attempt == 1


async def _validate_waiting_interaction(
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
    *,
    started_at: datetime,
) -> None:
    """Require one delivered, unclaimed form before the restart drill."""

    from sqlalchemy import select

    from api.db.db_models import (
        McpInteraction,
        McpInteractionCallbackReceipt,
        McpInteractionPresentation,
        McpInteractionResumeJob,
        async_session_factory,
    )
    from common.bootstrap import ensure_initialized

    ensure_initialized(initialize_resources=False)
    if async_session_factory is None:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "database")
    async with async_session_factory() as session:
        interactions = list(
            (
                await session.scalars(
                    select(McpInteraction).where(
                        McpInteraction.tenant_id == candidate.tenant_id,
                        McpInteraction.platform_user_id == candidate.platform_user_id,
                        McpInteraction.external_identity_id == candidate.external_identity_id,
                        McpInteraction.identity_revision == candidate.identity_revision,
                        McpInteraction.agent_id == candidate.agent_id,
                        McpInteraction.agent_revision_id == candidate.agent_revision_id,
                        McpInteraction.mcp_server_id == candidate.mcp_server_id,
                        McpInteraction.tool_name == "leave_preview_leave_form",
                        McpInteraction.state == "awaiting_input",
                        McpInteraction.created_at >= started_at,
                    ),
                )
            ).all(),
        )
        if len(interactions) != 1:
            raise OperatorError(
                ExitCode.LIVE_EVIDENCE_PENDING,
                "waiting_interaction_count",
            )
        interaction = interactions[0]
        policy = _read_json(layout.artifacts / "tool-policies.json")
        grant = _read_json(layout.artifacts / "mcp-grants.json")
        if (
            interaction.resource_name != RESOURCE_NAME
            or interaction.resource_uri != cast(dict[str, str], manifest["urls"])["gateway"]
            or interaction.policy_revision != policy.get("policy_revision")
            or interaction.credential_generation != grant.get("credential_generation")
            or interaction.effect != "prepare"
            or interaction.replay_mode != "reusable"
            or interaction.expires_at <= datetime.now(UTC)
        ):
            raise OperatorError(
                ExitCode.LIVE_EVIDENCE_PENDING,
                "waiting_interaction_authority",
            )
        presentations = list(
            (
                await session.scalars(
                    select(McpInteractionPresentation).where(
                        McpInteractionPresentation.interaction_id == interaction.id,
                    ),
                )
            ).all(),
        )
        if len(presentations) != 1:
            raise OperatorError(
                ExitCode.LIVE_EVIDENCE_PENDING,
                "waiting_form_count",
            )
        presentation = presentations[0]
        jobs = list(
            (
                await session.scalars(
                    select(McpInteractionResumeJob).where(
                        McpInteractionResumeJob.interaction_id == interaction.id,
                    ),
                )
            ).all(),
        )
        receipts = list(
            (
                await session.scalars(
                    select(McpInteractionCallbackReceipt).where(
                        McpInteractionCallbackReceipt.presentation_id == presentation.id,
                    ),
                )
            ).all(),
        )
        if (
            presentation.delivery_kind != "form"
            or presentation.delivery_state != "delivered"
            or presentation.response_state != "open"
            or presentation.safe_error_code is not None
            or presentation.delivery_attempt != 1
            or presentation.binding_id != candidate.binding_id
            or presentation.binding_generation != candidate.binding_generation
            or presentation.provider != "feishu"
            or presentation.provider_account_id != candidate.provider_account_id
            or jobs
            or receipts
        ):
            raise OperatorError(
                ExitCode.LIVE_EVIDENCE_PENDING,
                "waiting_form_state",
            )
        _validate_encrypted_seven_field_form(layout, interaction)


def _validate_encrypted_seven_field_form(
    layout: Layout,
    interaction: Any,
) -> None:
    from api.identity.mcp_interactions.crypto import (
        EncryptedInteractionPayload,
        InteractionPayloadCipher,
    )
    from common.mcp_interactions import decode_interaction_payload_key

    interaction_key = (layout.secrets / "interaction-payload-key.txt").read_text(encoding="ascii").strip()
    cipher = InteractionPayloadCipher(
        [decode_interaction_payload_key(interaction_key)],
    )
    input_requests = cipher.decrypt(
        tenant_id=interaction.tenant_id,
        interaction_id=interaction.id,
        revision=interaction.revision,
        purpose="input_requests",
        encrypted=EncryptedInteractionPayload(
            ciphertext=interaction.input_requests_ciphertext,
            key_id=interaction.input_requests_key_id,
        ),
    )
    if not isinstance(input_requests, dict) or len(input_requests) != 1:
        raise OperatorError(
            ExitCode.LIVE_EVIDENCE_PENDING,
            "form_request_count",
        )
    request = next(iter(input_requests.values()))
    if not isinstance(request, dict):
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "form_projection")
    params = request.get("params")
    schema = params.get("requestedSchema") if isinstance(params, dict) else None
    properties = schema.get("properties") if isinstance(schema, dict) else None
    required = schema.get("required") if isinstance(schema, dict) else None
    if (
        request.get("method") != "elicitation/create"
        or not isinstance(properties, dict)
        or set(properties) != LEAVE_FORM_FIELDS
        or type(required) is not list
        or len(required) != len(LEAVE_FORM_FIELDS)
        or set(required) != LEAVE_FORM_FIELDS
    ):
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "form_projection")


def _validate_encrypted_terminal_v2(
    layout: Layout,
    interaction: Any,
    projection: Mapping[str, Any],
) -> None:
    """Bind the redacted delivery projection to the encrypted strict v2 result."""

    from api.identity.mcp_interactions.crypto import (
        EncryptedInteractionPayload,
        InteractionPayloadCipher,
    )
    from common.mcp_interactions import decode_interaction_payload_key

    if interaction.result_ciphertext is None or interaction.result_key_id is None:
        raise OperatorError(
            ExitCode.LIVE_EVIDENCE_PENDING,
            "terminal_result_missing",
        )
    interaction_key = (layout.secrets / "interaction-payload-key.txt").read_text(encoding="ascii").strip()
    cipher = InteractionPayloadCipher(
        [decode_interaction_payload_key(interaction_key)],
    )
    result = cipher.decrypt(
        tenant_id=interaction.tenant_id,
        interaction_id=interaction.id,
        revision=interaction.revision,
        purpose="result",
        encrypted=EncryptedInteractionPayload(
            ciphertext=interaction.result_ciphertext,
            key_id=interaction.result_key_id,
        ),
    )
    if type(result) is not dict:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "terminal_result_shape")
    payload = result
    if set(payload) == {"result"}:
        wrapped = payload["result"]
        if type(wrapped) is not dict:
            raise OperatorError(
                ExitCode.LIVE_EVIDENCE_PENDING,
                "terminal_result_shape",
            )
        payload = wrapped
    if (
        set(payload)
        != {
            "kind",
            "version",
            "title",
            "message",
            "fields",
            "preview_only",
        }
        or payload.get("kind") != "com.ofmcp/interaction-terminal"
        or type(payload.get("version")) is not int
        or payload.get("version") != 2
        or payload.get("preview_only") is not True
        or projection
        != {
            "state": "completed",
            "title": payload.get("title"),
            "message": payload.get("message"),
            "fields": payload.get("fields"),
        }
    ):
        raise OperatorError(
            ExitCode.LIVE_EVIDENCE_PENDING,
            "terminal_result_v2",
        )


async def _live_database_checks(
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
    *,
    started_at: datetime,
) -> tuple[list[Check], datetime]:
    from sqlalchemy import select

    from api.db.db_models import (
        McpInteraction,
        McpInteractionCallbackReceipt,
        McpInteractionPresentation,
        McpInteractionResumeJob,
        async_session_factory,
    )
    from common.bootstrap import ensure_initialized

    ensure_initialized(initialize_resources=False)
    if async_session_factory is None:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "database")
    async with async_session_factory() as session:
        interactions = list(
            (
                await session.scalars(
                    select(McpInteraction)
                    .where(
                        McpInteraction.tenant_id == candidate.tenant_id,
                        McpInteraction.platform_user_id == candidate.platform_user_id,
                        McpInteraction.agent_id == candidate.agent_id,
                        McpInteraction.agent_revision_id == candidate.agent_revision_id,
                        McpInteraction.mcp_server_id == candidate.mcp_server_id,
                        McpInteraction.tool_name == "leave_preview_leave_form",
                        McpInteraction.created_at >= started_at,
                    )
                    .order_by(McpInteraction.created_at),
                )
            ).all(),
        )
        if len(interactions) != 1:
            raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "interaction_count")
        interaction = interactions[0]
        policy = _read_json(layout.artifacts / "tool-policies.json")
        grant = _read_json(layout.artifacts / "mcp-grants.json")
        if (
            interaction.external_identity_id != candidate.external_identity_id
            or interaction.identity_revision != candidate.identity_revision
            or interaction.resource_name != RESOURCE_NAME
            or interaction.resource_uri != cast(dict[str, str], manifest["urls"])["gateway"]
            or interaction.policy_revision != policy.get("policy_revision")
            or interaction.credential_generation != grant.get("credential_generation")
            or interaction.effect != "prepare"
            or interaction.replay_mode != "reusable"
        ):
            raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "interaction_authority")
        presentations = list(
            (
                await session.scalars(
                    select(McpInteractionPresentation).where(
                        McpInteractionPresentation.interaction_id == interaction.id,
                    ),
                )
            ).all(),
        )
        jobs = list(
            (
                await session.scalars(
                    select(McpInteractionResumeJob).where(
                        McpInteractionResumeJob.interaction_id == interaction.id,
                    ),
                )
            ).all(),
        )
        presentation_ids = [presentation.id for presentation in presentations]
        receipts = (
            list(
                (
                    await session.scalars(
                        select(McpInteractionCallbackReceipt).where(
                            McpInteractionCallbackReceipt.presentation_id.in_(presentation_ids),
                        ),
                    )
                ).all(),
            )
            if presentation_ids
            else []
        )
        terminal = [item for item in presentations if item.delivery_kind == "terminal" and item.delivery_state == "delivered" and item.response_state == "terminal" and item.safe_error_code is None]
        successful_jobs = [item for item in jobs if item.state == "succeeded" and item.safe_error_code is None]
        claimed_receipts = [item for item in receipts if item.state == "claimed" and item.safe_error_code is None]
        if (
            interaction.state != "completed"
            or len(presentations) != 1
            or len(jobs) != 1
            or len(receipts) != 1
            or len(terminal) != 1
            or len(successful_jobs) != 1
            or len(claimed_receipts) != 1
            or terminal[0].binding_id != candidate.binding_id
            or terminal[0].binding_generation != candidate.binding_generation
            or terminal[0].provider != "feishu"
            or terminal[0].provider_account_id != candidate.provider_account_id
            or terminal[0].tenant_id != candidate.tenant_id
            or terminal[0].revision != interaction.revision
            or successful_jobs[0].tenant_id != candidate.tenant_id
            or successful_jobs[0].revision != interaction.revision
            or claimed_receipts[0].binding_id != candidate.binding_id
            or not _valid_live_delivery_attempts(
                terminal_delivery_attempt=terminal[0].delivery_attempt,
                resume_attempt=successful_jobs[0].attempt,
                callback_attempt=claimed_receipts[0].attempt,
            )
        ):
            raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "interaction_terminal")
        terminal_projection = terminal[0].delivery_projection
        if (
            not isinstance(terminal_projection, dict)
            or set(terminal_projection) != {"state", "title", "message", "fields"}
            or terminal_projection.get("state") != "completed"
            or not isinstance(terminal_projection.get("title"), str)
            or not isinstance(terminal_projection.get("message"), str)
            or not isinstance(terminal_projection.get("fields"), list)
            or not 1 <= len(terminal_projection["fields"]) <= 8
            or any(
                not isinstance(field, dict) or set(field) != {"label", "value"} or not all(isinstance(value, str) and value.strip() for value in field.values())
                for field in terminal_projection["fields"]
            )
        ):
            raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "terminal_projection")
        _validate_encrypted_terminal_v2(
            layout,
            interaction,
            terminal_projection,
        )
        _validate_encrypted_seven_field_form(layout, interaction)
        completed_at = interaction.updated_at
    checks = [
        Check("live.interaction.exactly_once", "pass", "ok"),
        Check("live.callback.claimed_once", "pass", "ok"),
        Check("live.resume.succeeded_once", "pass", "ok"),
        Check("live.terminal.delivered", "pass", "ok"),
        Check("live.form.seven_fields", "pass", "ok"),
        Check("live.terminal.strict_v2_projection", "pass", "ok"),
        Check("live.identity.authority_bound", "pass", "ok"),
        Check("live.policy.grant_bound", "pass", "ok"),
    ]
    return checks, completed_at


def _a6_audit_high_water(layout: Layout) -> int:
    import psycopg

    _, dsn = _load_a6_dsn(layout)
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COALESCE(max(recorded_seq), 0) FROM ofmcp_security.audit_event",
                )
                value = cursor.fetchone()
    except Exception as exc:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_audit_reader") from exc
    if value is None or type(value[0]) is not int or value[0] < 0:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_audit_boundary")
    return value[0]


def _a6_prunable_replay_rows(layout: Layout) -> int:
    import psycopg

    _, dsn = _load_a6_dsn(layout)
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM ofmcp_security.replay_claim WHERE prunable_at <= ofmcp_security.now_epoch()",
                )
                value = cursor.fetchone()
    except Exception as exc:
        raise OperatorError(
            ExitCode.PREREQUISITE_MISSING,
            "a6_replay_prunable",
        ) from exc
    if value is None or type(value[0]) is not int or value[0] < 0:
        raise OperatorError(
            ExitCode.ARTIFACT_INVALID,
            "a6_replay_prunable",
        )
    return value[0]


def _a6_audit_correlation(
    layout: Layout,
    manifest: Mapping[str, Any],
    *,
    baseline_seq: int,
    end_seq: int,
    policy_revision: str,
) -> dict[str, object]:
    import psycopg

    _, dsn = _load_a6_dsn(layout)
    urls = cast(dict[str, str], manifest["urls"])
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT recorded_seq, recorded_at, event_type, trace_id,
                           mcp_call_id, principal_id_hash, tenant_id_hash,
                           agent_id_hash, client_id_hash, request_fingerprint,
                           decision, reason_code, effect, replay_mode,
                           policy_revision, token_issuer, token_audience,
                           tool_name
                    FROM ofmcp_security.audit_event
                    WHERE recorded_seq > %s
                      AND recorded_seq <= %s
                      AND tool_name IN (
                          'leave_preview_leave_form',
                          'leave_create_leave_draft',
                          'leave_submit_leave'
                      )
                    ORDER BY recorded_seq
                    """,
                    (baseline_seq, end_seq),
                )
                rows = cursor.fetchall()
    except Exception as exc:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_audit_reader") from exc
    # Any A6 execution-stage event for a write tool inside the sealed sequence
    # window invalidates the preview-only evidence.  The simulator counter is
    # an independent downstream boundary, not the sole proof.
    write_rows = [row for row in rows if row[17] in {"leave_create_leave_draft", "leave_submit_leave"}]
    if write_rows:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_write_event")
    preview_rows = [row for row in rows if row[17] == "leave_preview_leave_form"]
    groups: dict[tuple[str, str], list[tuple[object, ...]]] = {}
    for row in preview_rows:
        call_id = row[4]
        trace_id = row[3]
        if type(call_id) is not str or type(trace_id) is not str:
            raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_audit_shape")
        groups.setdefault((call_id, trace_id), []).append(row)
    # One logical invocation opens the form and one resumes it.  Each A6
    # reusable call emits exactly PRE_EXECUTION + successful OUTCOME.
    if len(groups) != 2:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_call_count")
    identities: set[tuple[object, object, object, object]] = set()
    ordered_groups = sorted(groups.values(), key=lambda group: cast(int, group[0][0]))
    for group in ordered_groups:
        if len(group) != 2:
            raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_stage_count")
        stages = [(row[2], row[10], row[11]) for row in group]
        if stages != [
            ("security_execution.pre_execution", "allow", "pre_execution_allowed"),
            ("security_execution.outcome", "allow", "execution_succeeded"),
        ]:
            raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_stage_pair")
        if any(
            row[12] != "prepare"
            or row[13] != "reusable"
            or row[14] != policy_revision
            or row[15] != urls["issuer"]
            or row[16] != urls["gateway"]
            or row[3] != group[0][3]
            or row[4] != group[0][4]
            or row[9] != group[0][9]
            for row in group
        ):
            raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_stage_correlation")
        identities.add((group[0][5], group[0][6], group[0][7], group[0][8]))
    if len(identities) != 1:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_identity_correlation")
    terminal_trace = ordered_groups[-1][0][3]
    if type(terminal_trace) is not str or len(terminal_trace) != 32:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_trace")
    trace_ids = [cast(str, group[0][3]) for group in ordered_groups]
    if any(not re.fullmatch(r"[0-9a-f]{32}", trace_id) for trace_id in trace_ids):
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_trace")
    return {
        "logical_call_count": len(groups),
        "audit_event_count": len(preview_rows),
        "write_tool_audit_event_count": len(write_rows),
        "sealed_end_recorded_seq": end_seq,
        "terminal_trace_id": terminal_trace,
        "trace_ids": trace_ids,
        "first_recorded_seq": ordered_groups[0][0][0],
        "last_recorded_seq": ordered_groups[-1][-1][0],
    }


def _mock_run_boundary(
    layout: Layout,
    manifest: Mapping[str, Any],
    *,
    expected: Mapping[str, object] | None = None,
) -> dict[str, object]:
    candidate = _selected_candidate(layout)
    spec = _build_specs(layout, manifest, candidate, api_stage="b")["mock"]
    record = ProcessManager(layout)._load_records().get("mock")
    if record is None:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "mock_ownership")
    ProcessManager(layout)._verify_record(record, spec)
    payload = _fetch_json(f"{cast(dict[str, str], manifest['urls'])['mock']}/healthz")
    calls = payload.get("calls")
    allowed_calls = ECOLOGY_READ_CALLS | {"doCreateRequest_blocked"}
    if not isinstance(calls, dict) or any(type(name) is not str or name not in allowed_calls or type(count) is not int or count < 0 for name, count in calls.items()):
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "mock_call_shape")
    blocked = calls.get("doCreateRequest_blocked", 0)
    if payload.get("write_enabled") is not False or type(blocked) is not int or blocked < 0:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "mock_boundary")
    boundary: dict[str, object] = {
        "pid": record.pid,
        "create_time": record.create_time,
        "do_create_request_blocked": blocked,
        "calls": dict(sorted(calls.items())),
    }
    if expected is not None:
        expected_calls = expected.get("calls")
        if (
            expected.get("pid") != boundary["pid"]
            or expected.get("create_time") != boundary["create_time"]
            or expected.get("do_create_request_blocked") != 0
            or boundary["do_create_request_blocked"] != 0
            or not isinstance(expected_calls, Mapping)
            or any(type(expected_calls.get(name, 0)) is not int or cast(int, calls.get(name, 0)) <= cast(int, expected_calls.get(name, 0)) for name in ECOLOGY_READ_CALLS)
        ):
            raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "mock_boundary_reset_or_write")
    return boundary


async def _multirag_migration_revision() -> str:
    from sqlalchemy import text

    from api.db.db_models import async_session_factory
    from common.bootstrap import ensure_initialized

    ensure_initialized(initialize_resources=False)
    if async_session_factory is None:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "database")
    async with async_session_factory() as session:
        value = await session.scalar(text("SELECT version_num FROM usr_ai.alembic_version"))
    if type(value) is not str or not value:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "multirag_migration")
    return value


def _a6_migration_revision(layout: Layout) -> str:
    import psycopg

    _, dsn = _load_a6_dsn(layout)
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version_num FROM ofmcp_security.alembic_version")
                row = cursor.fetchone()
    except Exception as exc:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_migration") from exc
    if row is None or type(row[0]) is not str or not row[0]:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "a6_migration")
    return row[0]


def _validate_a6_durability(layout: Layout) -> dict[str, object]:
    import psycopg

    _, dsn = _load_a6_dsn(layout)
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                      (SELECT count(*)
                         FROM pg_trigger t
                         JOIN pg_class c ON c.oid = t.tgrelid
                         JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'ofmcp_security'
                          AND c.relname = 'audit_event'
                          AND NOT t.tgisinternal
                          AND t.tgenabled = 'A'),
                      current_setting('synchronous_commit'),
                      (SELECT bool_and(c.relpersistence = 'p')
                         FROM pg_class c
                         JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'ofmcp_security'
                          AND c.relname IN ('audit_event', 'replay_claim'))
                    """,
                )
                row = cursor.fetchone()
    except Exception as exc:
        raise OperatorError(ExitCode.PREREQUISITE_MISSING, "a6_durability") from exc
    if row is None or row[0] != 2 or row[1] not in {"on", "remote_apply", "remote_write"} or row[2] is not True:
        raise OperatorError(ExitCode.AUTHORITY_STALE, "a6_durability")
    return {
        "always_enabled_guards": row[0],
        "synchronous_commit": row[1],
        "permanent_tables": row[2],
    }


def _validate_migration_heads(layout: Layout) -> dict[str, str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(REPOSITORY_ROOT / "configs" / "alembic"),
    )
    expected_multirag = ScriptDirectory.from_config(config).get_current_head()
    actual_multirag = asyncio.run(_multirag_migration_revision())
    actual_a6 = _a6_migration_revision(layout)
    if expected_multirag is None or actual_multirag != expected_multirag or actual_a6 != A6_MIGRATION_HEAD:
        raise OperatorError(ExitCode.AUTHORITY_STALE, "migration_head")
    return {"multirag": actual_multirag, "ofmcp_a6": actual_a6}


def _runtime_repository_evidence(
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
) -> dict[str, object]:
    specs = _build_specs(layout, manifest, candidate, api_stage="b")
    manager = ProcessManager(layout)
    records = manager._load_records()
    result: dict[str, object] = {}
    for name in PROCESS_ORDER:
        record = records.get(name)
        if record is None:
            raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "runtime_process_record")
        manager._verify_record(record, specs[name])
        result[name] = {
            "pid": record.pid,
            "create_time": record.create_time,
            "process_group": record.process_group,
            "cwd": record.cwd,
            "argv_sha256": record.argv_sha256,
            "repo_head_sha": record.repo_head_sha,
            "repo_dirty": record.repo_dirty,
            "repo_state_sha256": record.repo_state_sha256,
            "api_stage": record.api_stage,
            "overlay_sha256": record.overlay_sha256,
        }
    return result


def _private_file_offset(path: Path) -> int:
    if not path.exists():
        return 0
    _assert_regular(path, mode=0o600)
    return path.stat().st_size


def _read_json_lines_since(
    path: Path,
    *,
    offset: int,
    limit: int = 1_048_576,
) -> tuple[list[dict[str, Any]], int]:
    if type(offset) is not int or offset < 0:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "ledger_offset")
    if not path.exists():
        if offset:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "ledger_truncated")
        return [], 0
    _assert_regular(path, mode=0o600)
    size = path.stat().st_size
    if size < offset or size - offset > limit:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "ledger_window")
    with path.open("rb") as stream:
        stream.seek(offset)
        raw = stream.read(limit + 1)
    if len(raw) > limit or (raw and not raw.endswith(b"\n")):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "ledger_shape")
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "ledger_json") from exc
        if type(event) is not dict:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "ledger_event")
        events.append(cast(dict[str, Any], event))
    return events, size


def _validated_runtime_segments(
    *,
    initial: Mapping[str, object],
    completed: Mapping[str, object],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, object]]:
    if set(initial) != set(PROCESS_ORDER) or set(completed) != set(PROCESS_ORDER):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "runtime_boundary")
    expected: dict[str, object | None] = copy.deepcopy(dict(initial))
    normalized: list[dict[str, object]] = []
    for event in events:
        if (
            set(event) != {"event", "at", "record"}
            or event.get("event") not in {"started", "stopped"}
            or type(event.get("at")) is not str
            or _parse_evidence_time(event.get("at")) is None
            or not isinstance(event.get("record"), Mapping)
        ):
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "process_event_shape")
        record = dict(cast(Mapping[str, object], event["record"]))
        name = record.get("name")
        if type(name) is not str or name not in RESTARTABLE_DURING_PROBE:
            raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "runtime_nonrestartable_changed")
        comparable = {key: value for key, value in record.items() if key != "name"}
        if event["event"] == "stopped":
            if expected.get(name) != comparable:
                raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "runtime_segment_chain")
            expected[name] = None
        else:
            if expected.get(name) is not None:
                raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "runtime_segment_chain")
            expected[name] = comparable
        normalized.append(
            {
                "event": cast(str, event["event"]),
                "at": cast(str, event["at"]),
                "process": name,
                "record": comparable,
            },
        )
    if expected != dict(completed):
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "runtime_segment_end")
    return normalized


def _validate_waiting_restart_segments(
    segments: Sequence[Mapping[str, object]],
    *,
    stage_a_overlay_sha256: str,
    stage_b_overlay_sha256: str,
) -> None:
    expected = (
        ("stopped", "api"),
        ("started", "api"),
        ("stopped", "supervisor"),
        ("started", "supervisor"),
        ("stopped", "api"),
        ("started", "api"),
    )
    actual = tuple((segment.get("event"), segment.get("process")) for segment in segments)
    if actual != expected:
        raise OperatorError(
            ExitCode.LIVE_EVIDENCE_PENDING,
            "waiting_restart_segments",
        )
    records = [cast(Mapping[str, object], segment["record"]) for segment in segments]
    expected_bindings = (
        ("b", stage_b_overlay_sha256),
        ("a", stage_a_overlay_sha256),
        (None, None),
        (None, None),
        ("a", stage_a_overlay_sha256),
        ("b", stage_b_overlay_sha256),
    )
    if any(
        record.get("api_stage") != stage or record.get("overlay_sha256") != overlay_sha256
        for record, (stage, overlay_sha256) in zip(
            records,
            expected_bindings,
            strict=True,
        )
    ):
        raise OperatorError(
            ExitCode.LIVE_EVIDENCE_PENDING,
            "waiting_restart_stage_binding",
        )
    api_generations = {(records[index].get("pid"), records[index].get("create_time")) for index in (0, 1, 5)}
    supervisor_generations = {(records[index].get("pid"), records[index].get("create_time")) for index in (2, 3)}
    if len(api_generations) != 3 or len(supervisor_generations) != 2:
        raise OperatorError(
            ExitCode.LIVE_EVIDENCE_PENDING,
            "waiting_restart_process_generation",
        )


def _probe_static_boundary(
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
) -> dict[str, object]:
    _require_immutable_implementation_repositories(manifest)
    policy = _read_json(layout.artifacts / "tool-policies.json")
    grant = _read_json(layout.artifacts / "mcp-grants.json")
    repositories = cast(dict[str, str], manifest["repositories"])
    pki = cast(dict[str, Any], manifest["pki"])
    artifacts = {
        name: _sha256_file(layout.artifacts / name)
        for name in (
            "api-stage-a.yaml",
            "api-stage-b.yaml",
            "jwks-public-keys.json",
            "mcp-grants.json",
            "tool-policies.json",
        )
    }
    return {
        "deployment_manifest_sha256": _sha256_file(layout.deployment),
        "selection_sha256": _sha256_file(layout.selection),
        "candidate_ref": candidate.ref,
        "binding_generation": candidate.binding_generation,
        "identity_revision": candidate.identity_revision,
        "policy_revision": policy.get("policy_revision"),
        "grant_revision": grant.get("grant_revision"),
        "credential_generation": grant.get("credential_generation"),
        "repositories": {
            "multirag": _repo_state(Path(repositories["multirag"])),
            "ofmcp": _repo_state(Path(repositories["ofmcp"])),
        },
        "migration_revisions": _validate_migration_heads(layout),
        "certificates": {
            "ca_sha256": pki["ca"]["sha256"],
            "issuer_sha256": pki["leaves"]["issuer"]["sha256"],
            "gateway_sha256": pki["leaves"]["gateway"]["sha256"],
        },
        "key_fingerprints": _key_fingerprint_evidence(layout, manifest),
        "artifact_sha256": artifacts,
    }


def _runtime_repository_binding_matches(
    runtime: Mapping[str, object],
    static_boundary: Mapping[str, object],
) -> bool:
    repositories = static_boundary.get("repositories")
    if not isinstance(repositories, Mapping):
        return False
    for name, value in runtime.items():
        if not isinstance(value, Mapping):
            return False
        repository_name = "multirag" if name in {"jwks", "api", "supervisor"} else "ofmcp"
        repository = repositories.get(repository_name)
        if (
            not isinstance(repository, Mapping)
            or value.get("repo_head_sha") != repository.get("head_sha")
            or value.get("repo_dirty") != repository.get("dirty")
            or value.get("repo_state_sha256") != repository.get("worktree_sha256")
        ):
            return False
    return True


def _validate_runtime_repository_binding(
    runtime: Mapping[str, object],
    static_boundary: Mapping[str, object],
) -> None:
    if not _runtime_repository_binding_matches(runtime, static_boundary):
        raise OperatorError(
            ExitCode.LIVE_EVIDENCE_PENDING,
            "runtime_repository_binding",
        )


def _delegated_trace_ids_since(layout: Layout, *, offset: int) -> tuple[list[str], int]:
    path = layout.logs / "api.log"
    if not path.exists():
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "api_trace_log")
    _assert_regular(path, mode=0o600)
    size = path.stat().st_size
    if size < offset or size - offset > 8_388_608:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "api_trace_window")
    with path.open("rb") as stream:
        stream.seek(offset)
        raw = stream.read(8_388_609)
    if len(raw) > 8_388_608:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "api_trace_window")
    pattern = re.compile(
        rb"mcp_delegation_event=trace_context tool=leave_preview_leave_form trace_id=([0-9a-f]{32})(?![0-9a-f])",
    )
    return [match.group(1).decode("ascii") for match in pattern.finditer(raw)], size


def _require_sha256(value: object, *, detail: str) -> str:
    if not _is_sha256(value):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, detail)
    return cast(str, value)


def _is_sha256(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _require_sha256_list(value: object, *, detail: str) -> list[str]:
    if type(value) is not list or not value:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, detail)
    return [_require_sha256(item, detail=detail) for item in value]


def _key_fingerprint_evidence(
    layout: Layout,
    manifest: Mapping[str, Any],
) -> dict[str, object]:
    secrets_manifest = cast(dict[str, Any], manifest["secrets"])
    channel = cast(dict[str, Any], secrets_manifest["channel"])
    interaction = cast(dict[str, Any], secrets_manifest["interaction"])
    p3 = cast(dict[str, Any], secrets_manifest["p3"])
    ecology_mock = cast(dict[str, Any], secrets_manifest["ecology_mock"])
    key_ids = channel.get("key_ids")
    if type(key_ids) is not list or not key_ids or any(type(item) is not str or not item for item in key_ids):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_fingerprints")
    channel_fingerprints = _require_sha256_list(
        channel.get("key_fingerprints"),
        detail="channel_fingerprints",
    )
    if len(key_ids) != len(channel_fingerprints):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "channel_fingerprints")

    ofmcp_manifest_path = Path(cast(str, secrets_manifest["ofmcp_manifest"]))
    if ofmcp_manifest_path != layout.root / "ofmcp-deployment.json":
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "ofmcp_manifest_path")
    ofmcp_manifest = _read_json(ofmcp_manifest_path)
    if ofmcp_manifest.get("schema") != "com.ofmcp/ops-deployment" or ofmcp_manifest.get("version") != 1:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "ofmcp_manifest")
    ofmcp_secrets = ofmcp_manifest.get("secrets")
    if not isinstance(ofmcp_secrets, dict):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "ofmcp_fingerprints")
    request_state = ofmcp_secrets.get("request_state_key_ring")
    a6_fingerprint = ofmcp_secrets.get("a6_fingerprint_key_ring")
    a6_identity = ofmcp_secrets.get("a6_identity_key")
    if not all(isinstance(item, dict) for item in (request_state, a6_fingerprint, a6_identity)):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "ofmcp_fingerprints")
    request_state = cast(dict[str, Any], request_state)
    a6_fingerprint = cast(dict[str, Any], a6_fingerprint)
    a6_identity = cast(dict[str, Any], a6_identity)
    return {
        "channel_secret_ring": {
            "active_key_id": channel.get("active_key_id"),
            "key_ids": key_ids,
            "fingerprints": channel_fingerprints,
        },
        "interaction_payload": {
            "fingerprint": _require_sha256(
                interaction.get("key_fingerprint"),
                detail="interaction_fingerprint",
            ),
        },
        "p3_signing": {
            "kid": p3.get("kid"),
            "public_key_fingerprint": _require_sha256(
                p3.get("public_key_fingerprint"),
                detail="p3_fingerprint",
            ),
        },
        "ecology_simulator": {
            "public_key_fingerprint": _require_sha256(
                ecology_mock.get("public_key_fingerprint"),
                detail="mock_fingerprint",
            ),
        },
        "ofmcp_request_state": {
            "fingerprints": _require_sha256_list(
                request_state.get("fingerprints"),
                detail="request_state_fingerprints",
            ),
        },
        "ofmcp_a6_fingerprint": {
            "fingerprints": _require_sha256_list(
                a6_fingerprint.get("fingerprints"),
                detail="a6_fingerprints",
            ),
        },
        "ofmcp_a6_identity": {
            "fingerprint": _require_sha256(
                a6_identity.get("fingerprint"),
                detail="a6_identity_fingerprint",
            ),
        },
    }


def _fixture_evidence(
    ofmcp_repo: Path,
    *,
    repository_state: Mapping[str, object],
) -> dict[str, object]:
    service_manifest = ofmcp_repo / "services" / "leave" / "service.toml"
    simulator_source = ofmcp_repo / "services" / "leave" / "src" / "ofmcp" / "services" / "leave" / "mock_ecology.py"
    contract_snapshot = ofmcp_repo / "services" / "leave" / "contract" / "fingerprints.json"
    try:
        with service_manifest.open("rb") as stream:
            service = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "fixture_manifest") from exc
    contract_version = service.get("contract_version")
    if type(contract_version) is not str or not contract_version:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "fixture_version")
    return {
        "name": "ofmcp-ecology-mock",
        "write_enabled": False,
        "leave_contract_version": contract_version,
        "repository_head_sha": repository_state["head_sha"],
        "repository_state_sha256": repository_state["worktree_sha256"],
        "simulator_source_sha256": _sha256_file(simulator_source),
        "contract_snapshot_sha256": _sha256_file(contract_snapshot),
    }


def _write_evidence(
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
    *,
    started_at: datetime,
    completed_at: datetime,
    audit_correlation: Mapping[str, object],
    mock_boundary_start: Mapping[str, object],
    mock_boundary_end: Mapping[str, object],
    static_boundary: Mapping[str, object],
    runtime_start: Mapping[str, object],
    runtime_end: Mapping[str, object],
    runtime_segments: Sequence[Mapping[str, object]],
    waiting_restart: Mapping[str, object],
    process_event_end_offset: int,
    trace_ids: Sequence[str],
    live_checks: Sequence[Check],
) -> Path:
    if _probe_static_boundary(layout, manifest, candidate) != static_boundary:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "static_boundary_changed")
    if not _valid_waiting_restart_summary(
        waiting_restart,
        runtime_segments=runtime_segments,
        expected_artifact_sha256=cast(
            Mapping[str, object],
            static_boundary["artifact_sha256"],
        ),
        runtime_started=runtime_start,
        runtime_completed=runtime_end,
        run_started_at=started_at,
        run_completed_at=completed_at,
        mock_run_start=mock_boundary_start,
        mock_run_end=mock_boundary_end,
    ):
        raise OperatorError(
            ExitCode.LIVE_EVIDENCE_PENDING,
            "waiting_restart_changed",
        )
    if _runtime_repository_evidence(layout, manifest, candidate) != runtime_end:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "runtime_boundary_changed")
    _validate_runtime_repository_binding(runtime_start, static_boundary)
    _validate_runtime_repository_binding(runtime_end, static_boundary)
    for segment in runtime_segments:
        _validate_runtime_repository_binding(
            {cast(str, segment["process"]): segment["record"]},
            static_boundary,
        )
    if _private_file_offset(layout.run / "process-events.ndjson") != process_event_end_offset:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "runtime_ledger_changed")
    run_id = f"eim-o5-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    directory = layout.evidence / run_id
    directory.mkdir(mode=0o700)
    repositories = cast(dict[str, str], manifest["repositories"])
    repository_evidence = cast(dict[str, object], static_boundary["repositories"])
    sealed_at = _now()
    run_document = {
        "schema": f"{SCHEMA}/evidence-run",
        "version": VERSION,
        "run_id": run_id,
        "status": "passed",
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "candidate_ref": candidate.ref,
        "repositories": repository_evidence,
        "runtime_implementations": runtime_end,
        "runtime_boundary": {
            "started": runtime_start,
            "completed": runtime_end,
            "segments": list(runtime_segments),
        },
        "waiting_restart": dict(waiting_restart),
        "migration_revisions": static_boundary["migration_revisions"],
        "deployment_manifest_sha256": static_boundary["deployment_manifest_sha256"],
        "selection_sha256": static_boundary["selection_sha256"],
        "binding_generation": static_boundary["binding_generation"],
        "identity_revision": static_boundary["identity_revision"],
        "policy_revision": static_boundary["policy_revision"],
        "grant_revision": static_boundary["grant_revision"],
        "credential_generation": static_boundary["credential_generation"],
        "certificates": static_boundary["certificates"],
        "key_fingerprints": static_boundary["key_fingerprints"],
        "artifact_sha256": static_boundary["artifact_sha256"],
        "fixture": _fixture_evidence(
            Path(repositories["ofmcp"]),
            repository_state=cast(
                Mapping[str, object],
                repository_evidence["ofmcp"],
            ),
        ),
        "trace_scope": "multirag_to_ofmcp",
        "cross_repository_trace_verified": True,
        "trace_ids": list(trace_ids),
        "trace_id": audit_correlation["terminal_trace_id"],
        "a6_audit": dict(audit_correlation),
        "ecology_write_boundary": {
            "pid": mock_boundary_start["pid"],
            "create_time": mock_boundary_start["create_time"],
            "blocked_at_start": mock_boundary_start["do_create_request_blocked"],
            "blocked_at_end": mock_boundary_end["do_create_request_blocked"],
            "calls_at_start": mock_boundary_start["calls"],
            "calls_at_end": mock_boundary_end["calls"],
            "read_call_deltas": {
                name: cast(dict[str, int], mock_boundary_end["calls"]).get(name, 0) - cast(dict[str, int], mock_boundary_start["calls"]).get(name, 0) for name in sorted(ECOLOGY_READ_CALLS)
            },
        },
    }
    checks = [
        *live_checks,
        Check("live.ecology.write_calls_zero", "pass", "ok"),
        Check("live.a6.stage_pairs_correlated", "pass", "ok"),
        Check("live.trace.cross_repository", "pass", "ok"),
        Check("live.runtime.segment_ledger_verified", "pass", "ok"),
        Check("live.runtime.wait_restart_verified", "pass", "ok"),
        Check("live.runtime.implementation_state_recorded", "pass", "ok"),
        Check("live.migrations.revisions_recorded", "pass", "ok"),
    ]
    _write_json(directory / "run.json", run_document)
    _write_json(
        directory / "checks.json",
        {
            "schema": f"{SCHEMA}/evidence-checks",
            "version": VERSION,
            "checks": [asdict(check) for check in checks],
        },
    )
    timeline_events = [
        {"event": "live_probe_started", "at": started_at.isoformat(), "run_id": run_id},
        *[
            {
                "event": f"runtime_process_{segment['event']}",
                "at": segment["at"],
                "process": segment["process"],
                "run_id": run_id,
            }
            for segment in runtime_segments
        ],
        {"event": "interaction_completed", "at": completed_at.isoformat(), "run_id": run_id},
        {"event": "evidence_sealed", "at": sealed_at, "run_id": run_id},
    ]
    _atomic_write(
        directory / "timeline.ndjson",
        b"".join(_canonical_bytes(item) + b"\n" for item in timeline_events),
    )
    candidate_specs = _build_specs(layout, manifest, candidate, api_stage="b")
    _write_json(
        directory / "logs-safe.json",
        {
            "schema": f"{SCHEMA}/safe-log-projection",
            "version": VERSION,
            "processes": ProcessManager(layout).snapshot(candidate_specs),
            "delegated_trace_ids": list(trace_ids),
        },
    )
    checksum_lines = []
    for name in ("checks.json", "logs-safe.json", "run.json", "timeline.ndjson"):
        checksum_lines.append(f"{_sha256_file(directory / name)}  {name}\n")
    _atomic_write(directory / "SHA256SUMS", "".join(checksum_lines).encode("ascii"))
    return directory


def _evidence_sensitive_values(layout: Layout) -> set[bytes]:
    values: set[bytes] = {
        b"Bearer ",
        b"postgresql://",
        b"postgresql+psycopg://",
        b"-----BEGIN PRIVATE KEY-----",
        b"-----BEGIN EC PRIVATE KEY-----",
    }
    if (layout.run / "discovery.json").exists():
        for candidate in _load_candidates(layout):
            for name, value in candidate.sensitive_document().items():
                if name == "ref" or type(value) is int:
                    continue
                rendered = str(value).encode()
                if len(rendered) >= 8:
                    values.add(rendered)
    for path in layout.secrets.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 65_536:
            raw = path.read_bytes().strip()
            if len(raw) >= 8:
                values.add(raw)
            for line in raw.splitlines():
                _, separator, value = line.partition(b"=")
                if separator and len(value) >= 8:
                    values.add(value)
    return values


def _valid_key_fingerprint_evidence(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "channel_secret_ring",
        "interaction_payload",
        "p3_signing",
        "ecology_simulator",
        "ofmcp_request_state",
        "ofmcp_a6_fingerprint",
        "ofmcp_a6_identity",
    }:
        return False
    channel = value["channel_secret_ring"]
    interaction = value["interaction_payload"]
    p3 = value["p3_signing"]
    mock = value["ecology_simulator"]
    request_state = value["ofmcp_request_state"]
    a6_fingerprint = value["ofmcp_a6_fingerprint"]
    a6_identity = value["ofmcp_a6_identity"]
    if not all(
        isinstance(item, dict)
        for item in (
            channel,
            interaction,
            p3,
            mock,
            request_state,
            a6_fingerprint,
            a6_identity,
        )
    ):
        return False
    channel = cast(dict[str, object], channel)
    key_ids = channel.get("key_ids")
    channel_digests = channel.get("fingerprints")
    return (
        type(channel.get("active_key_id")) is str
        and type(key_ids) is list
        and bool(key_ids)
        and all(type(item) is str and bool(item) for item in key_ids)
        and type(channel_digests) is list
        and len(channel_digests) == len(key_ids)
        and all(_is_sha256(item) for item in channel_digests)
        and _is_sha256(cast(dict[str, object], interaction).get("fingerprint"))
        and type(cast(dict[str, object], p3).get("kid")) is str
        and _is_sha256(
            cast(dict[str, object], p3).get("public_key_fingerprint"),
        )
        and _is_sha256(
            cast(dict[str, object], mock).get("public_key_fingerprint"),
        )
        and type(cast(dict[str, object], request_state).get("fingerprints")) is list
        and bool(cast(dict[str, object], request_state).get("fingerprints"))
        and all(
            _is_sha256(item)
            for item in cast(
                list[object],
                cast(dict[str, object], request_state)["fingerprints"],
            )
        )
        and type(cast(dict[str, object], a6_fingerprint).get("fingerprints")) is list
        and bool(cast(dict[str, object], a6_fingerprint).get("fingerprints"))
        and all(
            _is_sha256(item)
            for item in cast(
                list[object],
                cast(dict[str, object], a6_fingerprint)["fingerprints"],
            )
        )
        and _is_sha256(cast(dict[str, object], a6_identity).get("fingerprint"))
    )


def _parse_evidence_time(value: object) -> datetime | None:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _valid_runtime_record(item: object, *, process_name: str) -> bool:
    expected_fields = {
        "pid",
        "create_time",
        "process_group",
        "cwd",
        "argv_sha256",
        "repo_head_sha",
        "repo_dirty",
        "repo_state_sha256",
        "api_stage",
        "overlay_sha256",
    }
    common_valid = bool(
        isinstance(item, dict)
        and set(item) == expected_fields
        and type(item["pid"]) is int
        and item["pid"] > 1
        and type(item["process_group"]) is int
        and item["process_group"] > 1
        and item["process_group"] == item["pid"]
        and type(item["create_time"]) in {int, float}
        and type(item["cwd"]) is str
        and Path(item["cwd"]).is_absolute()
        and _is_sha256(item["argv_sha256"])
        and type(item["repo_head_sha"]) is str
        and re.fullmatch(r"[0-9a-f]{40}", item["repo_head_sha"])
        and type(item["repo_dirty"]) is bool
        and _is_sha256(item["repo_state_sha256"])
    )
    if not common_valid:
        return False
    typed_item = cast(dict[str, object], item)
    if process_name == "api":
        return typed_item["api_stage"] in {"a", "b"} and _is_sha256(
            typed_item["overlay_sha256"],
        )
    return typed_item["api_stage"] is None and typed_item["overlay_sha256"] is None


def _valid_runtime_snapshot(value: object) -> bool:
    return isinstance(value, dict) and set(value) == set(PROCESS_ORDER) and all(_valid_runtime_record(item, process_name=name) for name, item in value.items())


def _nonrestartable_runtime_sha256(value: Mapping[str, object]) -> str:
    return _sha256_bytes(
        _canonical_bytes(
            {name: value[name] for name in ("jwks", "mock", "gateway")},
        ),
    )


def _valid_mock_restart_checkpoint(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "pid",
        "create_time",
        "do_create_request_blocked",
        "calls",
    }:
        return False
    calls = value.get("calls")
    return bool(
        type(value.get("pid")) is int
        and cast(int, value["pid"]) > 1
        and type(value.get("create_time")) in {int, float}
        and value.get("do_create_request_blocked") == 0
        and isinstance(calls, dict)
        and all(type(name) is str and name in ECOLOGY_READ_CALLS | {"doCreateRequest_blocked"} and type(count) is int and count >= 0 for name, count in calls.items())
    )


def _valid_waiting_restart_summary(
    value: object,
    *,
    runtime_segments: Sequence[Mapping[str, object]],
    expected_artifact_sha256: Mapping[str, object],
    runtime_started: Mapping[str, object],
    runtime_completed: Mapping[str, object],
    run_started_at: datetime,
    run_completed_at: datetime,
    mock_run_start: Mapping[str, object],
    mock_run_end: Mapping[str, object],
) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "status",
        "started_at",
        "completed_at",
        "stage_a_overlay_sha256",
        "stage_b_overlay_sha256",
        "nonrestartable_before_sha256",
        "nonrestartable_after_sha256",
        "mock_before",
        "mock_after",
        "segments",
    }:
        return False
    started_at = _parse_evidence_time(value.get("started_at"))
    completed_at = _parse_evidence_time(value.get("completed_at"))
    raw_segments = value.get("segments")
    if not _valid_runtime_snapshot(runtime_started) or not _valid_runtime_snapshot(runtime_completed):
        return False
    expected_stage_a = expected_artifact_sha256.get("api-stage-a.yaml")
    expected_stage_b = expected_artifact_sha256.get("api-stage-b.yaml")
    expected_nonrestartable_before = _nonrestartable_runtime_sha256(runtime_started)
    expected_nonrestartable_after = _nonrestartable_runtime_sha256(runtime_completed)
    mock_before = value.get("mock_before")
    mock_after = value.get("mock_after")
    if (
        value.get("status") != "complete"
        or started_at is None
        or completed_at is None
        or not (run_started_at <= started_at < completed_at <= run_completed_at)
        or value.get("stage_a_overlay_sha256") != expected_stage_a
        or value.get("stage_b_overlay_sha256") != expected_stage_b
        or not _is_sha256(expected_stage_a)
        or not _is_sha256(expected_stage_b)
        or value.get("nonrestartable_before_sha256") != expected_nonrestartable_before
        or value.get("nonrestartable_after_sha256") != expected_nonrestartable_after
        or expected_nonrestartable_before != expected_nonrestartable_after
        or not _valid_mock_restart_checkpoint(mock_before)
        or mock_before != mock_after
        or not _valid_mock_restart_checkpoint(mock_after)
        or type(raw_segments) is not list
        or any(
            type(segment) is not dict
            or set(segment) != {"event", "at", "process", "record"}
            or type(segment.get("process")) is not str
            or _parse_evidence_time(segment.get("at")) is None
            or not _valid_runtime_record(
                segment.get("record"),
                process_name=cast(str, segment.get("process")),
            )
            for segment in raw_segments
        )
    ):
        return False
    typed_segments = cast(list[dict[str, object]], raw_segments)
    try:
        _validate_waiting_restart_segments(
            typed_segments,
            stage_a_overlay_sha256=cast(str, expected_stage_a),
            stage_b_overlay_sha256=cast(str, expected_stage_b),
        )
    except OperatorError:
        return False
    segment_times = [cast(datetime, _parse_evidence_time(segment["at"])) for segment in typed_segments]
    runtime_started_dict = cast(dict[str, object], runtime_started)
    runtime_completed_dict = cast(dict[str, object], runtime_completed)
    mock_before_dict = cast(dict[str, object], mock_before)
    if (
        list(runtime_segments) != typed_segments
        or not (started_at <= segment_times[0] and all(left < right for left, right in pairwise(segment_times)) and segment_times[-1] <= completed_at)
        or typed_segments[0]["record"] != runtime_started_dict["api"]
        or typed_segments[2]["record"] != runtime_started_dict["supervisor"]
        or typed_segments[3]["record"] != runtime_completed_dict["supervisor"]
        or typed_segments[5]["record"] != runtime_completed_dict["api"]
        or mock_before_dict["pid"] != cast(dict[str, object], runtime_started_dict["mock"])["pid"]
        or mock_before_dict["create_time"] != cast(dict[str, object], runtime_started_dict["mock"])["create_time"]
        or cast(dict[str, object], runtime_started_dict["mock"])["pid"] != cast(dict[str, object], runtime_completed_dict["mock"])["pid"]
        or cast(dict[str, object], runtime_started_dict["mock"])["create_time"] != cast(dict[str, object], runtime_completed_dict["mock"])["create_time"]
    ):
        return False
    point_calls = cast(dict[str, int], mock_before_dict["calls"])
    start_calls = mock_run_start.get("calls")
    end_calls = mock_run_end.get("calls")
    return bool(
        _valid_mock_restart_checkpoint(mock_run_start)
        and _valid_mock_restart_checkpoint(mock_run_end)
        and isinstance(start_calls, Mapping)
        and isinstance(end_calls, Mapping)
        and all(cast(int, start_calls.get(name, 0)) <= point_calls.get(name, 0) <= cast(int, end_calls.get(name, 0)) for name in ECOLOGY_READ_CALLS | {"doCreateRequest_blocked"})
    )


def _has_forbidden_evidence_key(value: object) -> bool:
    forbidden = SENSITIVE_KEYS | {
        "agent_id",
        "external_identity_id",
        "platform_user_id",
        "provider_account_id",
        "provider_tenant",
        "subject_value",
        "tenant_id",
    }
    if isinstance(value, Mapping):
        return any(str(key).casefold() in forbidden or _has_forbidden_evidence_key(nested) for key, nested in value.items())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return any(_has_forbidden_evidence_key(item) for item in value)
    return False


def verify_evidence(path: Path) -> dict[str, object]:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "evidence_path")
    expected = {"SHA256SUMS", "checks.json", "logs-safe.json", "run.json", "timeline.ndjson"}
    actual = {item.name for item in path.iterdir()}
    if actual != expected or _path_mode(path) != 0o700:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "evidence_shape")
    for name in expected:
        _assert_regular(path / name, mode=0o600)
    sums: dict[str, str] = {}
    for line in (path / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or name in sums or not _is_sha256(digest):
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "checksums")
        sums[name] = digest
    if set(sums) != expected - {"SHA256SUMS"}:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "checksum_set")
    for name, digest in sums.items():
        if not secrets.compare_digest(_sha256_file(path / name), digest):
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "checksum_mismatch")

    root = path.parent.parent
    layout, _ = _load_deployment(root)
    combined = b"\n".join((path / name).read_bytes() for name in sorted(expected))
    jwt_pattern = re.compile(rb"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
    if (
        any(value in combined for value in _evidence_sensitive_values(layout))
        or jwt_pattern.search(combined)
        or re.search(rb"postgres(?:ql)?(?:\+psycopg)?://", combined, re.IGNORECASE)
        or re.search(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----", combined)
    ):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "evidence_sensitive")

    run = _read_json(path / "run.json")
    checks = _read_json(path / "checks.json")
    logs = _read_json(path / "logs-safe.json")
    run_fields = {
        "schema",
        "version",
        "run_id",
        "status",
        "started_at",
        "completed_at",
        "candidate_ref",
        "repositories",
        "runtime_implementations",
        "runtime_boundary",
        "waiting_restart",
        "migration_revisions",
        "deployment_manifest_sha256",
        "selection_sha256",
        "binding_generation",
        "identity_revision",
        "policy_revision",
        "grant_revision",
        "credential_generation",
        "certificates",
        "key_fingerprints",
        "artifact_sha256",
        "fixture",
        "trace_scope",
        "cross_repository_trace_verified",
        "trace_ids",
        "trace_id",
        "a6_audit",
        "ecology_write_boundary",
    }
    started_at = _parse_evidence_time(run.get("started_at"))
    completed_at = _parse_evidence_time(run.get("completed_at"))
    if (
        set(run) != run_fields
        or run.get("schema") != f"{SCHEMA}/evidence-run"
        or run.get("version") != VERSION
        or run.get("status") != "passed"
        or run.get("run_id") != path.name
        or not re.fullmatch(r"eim-o5-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", path.name)
        or started_at is None
        or completed_at is None
        or completed_at < started_at
        or not re.fullmatch(r"candidate-[0-9a-f]{20}", str(run.get("candidate_ref")))
        or not _is_sha256(run.get("deployment_manifest_sha256"))
        or not _is_sha256(run.get("selection_sha256"))
        or not _is_sha256(run.get("policy_revision"))
        or not _is_sha256(run.get("grant_revision"))
        or type(run.get("binding_generation")) is not int
        or cast(int, run["binding_generation"]) < 1
        or type(run.get("identity_revision")) is not int
        or cast(int, run["identity_revision"]) < 1
        or type(run.get("credential_generation")) is not int
        or cast(int, run["credential_generation"]) < 1
        or _has_forbidden_evidence_key(run)
    ):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "evidence_run")

    repositories = run["repositories"]
    runtime = run["runtime_implementations"]
    runtime_boundary = run["runtime_boundary"]
    waiting_restart = run["waiting_restart"]
    migrations = run["migration_revisions"]
    audit = run["a6_audit"]
    write_boundary = run["ecology_write_boundary"]
    key_fingerprints = run["key_fingerprints"]
    fixture = run["fixture"]
    certificates = run["certificates"]
    trace_ids = run["trace_ids"]
    artifact_sha256 = run["artifact_sha256"]
    if (
        not isinstance(repositories, dict)
        or set(repositories) != {"multirag", "ofmcp"}
        or any(
            not isinstance(item, dict)
            or set(item) != {"dirty", "head_sha", "worktree_sha256"}
            or type(item["dirty"]) is not bool
            or not re.fullmatch(r"[0-9a-f]{40}", str(item["head_sha"]))
            or not _is_sha256(item["worktree_sha256"])
            for item in repositories.values()
        )
        or not _valid_runtime_snapshot(runtime)
        or not _runtime_repository_binding_matches(
            cast(Mapping[str, object], runtime),
            {"repositories": repositories},
        )
        or not isinstance(runtime_boundary, dict)
        or set(runtime_boundary) != {"started", "completed", "segments"}
        or not _valid_runtime_snapshot(runtime_boundary["started"])
        or not _valid_runtime_snapshot(runtime_boundary["completed"])
        or runtime_boundary["completed"] != runtime
        or not _runtime_repository_binding_matches(
            cast(Mapping[str, object], runtime_boundary["started"]),
            {"repositories": repositories},
        )
        or type(runtime_boundary["segments"]) is not list
        or any(
            type(item) is not dict
            or set(item) != {"event", "at", "process", "record"}
            or item["event"] not in {"started", "stopped"}
            or item["process"] not in RESTARTABLE_DURING_PROBE
            or _parse_evidence_time(item["at"]) is None
            or not _valid_runtime_record(
                item["record"],
                process_name=cast(str, item["process"]),
            )
            for item in runtime_boundary["segments"]
        )
        or any(
            not _runtime_repository_binding_matches(
                {cast(str, item["process"]): item["record"]},
                {"repositories": repositories},
            )
            for item in runtime_boundary["segments"]
        )
        or not isinstance(migrations, dict)
        or set(migrations) != {"multirag", "ofmcp_a6"}
        or migrations.get("ofmcp_a6") != A6_MIGRATION_HEAD
        or not all(isinstance(value, str) and value for value in migrations.values())
        or not isinstance(audit, dict)
        or set(audit)
        != {
            "logical_call_count",
            "audit_event_count",
            "write_tool_audit_event_count",
            "sealed_end_recorded_seq",
            "terminal_trace_id",
            "trace_ids",
            "first_recorded_seq",
            "last_recorded_seq",
        }
        or audit.get("logical_call_count") != 2
        or audit.get("audit_event_count") != 4
        or audit.get("write_tool_audit_event_count") != 0
        or any(
            type(audit.get(name)) is not int
            for name in (
                "sealed_end_recorded_seq",
                "first_recorded_seq",
                "last_recorded_seq",
            )
        )
        or not (0 < cast(int, audit["first_recorded_seq"]) <= cast(int, audit["last_recorded_seq"]) <= cast(int, audit["sealed_end_recorded_seq"]))
        or type(trace_ids) is not list
        or len(trace_ids) != 2
        or any(not re.fullmatch(r"[0-9a-f]{32}", str(value)) for value in trace_ids)
        or audit.get("trace_ids") != trace_ids
        or audit.get("terminal_trace_id") != trace_ids[-1]
        or run.get("trace_id") != trace_ids[-1]
        or run.get("trace_scope") != "multirag_to_ofmcp"
        or run.get("cross_repository_trace_verified") is not True
        or not isinstance(write_boundary, dict)
        or set(write_boundary)
        != {
            "pid",
            "create_time",
            "blocked_at_start",
            "blocked_at_end",
            "calls_at_start",
            "calls_at_end",
            "read_call_deltas",
        }
        or type(write_boundary.get("pid")) is not int
        or type(write_boundary.get("create_time")) not in {int, float}
        or write_boundary.get("pid") != cast(dict[str, Any], runtime)["mock"]["pid"]
        or write_boundary.get("create_time") != cast(dict[str, Any], runtime)["mock"]["create_time"]
        or write_boundary.get("blocked_at_start") != 0
        or write_boundary.get("blocked_at_end") != 0
        or not isinstance(write_boundary.get("calls_at_start"), dict)
        or not isinstance(write_boundary.get("calls_at_end"), dict)
        or not isinstance(write_boundary.get("read_call_deltas"), dict)
        or any(
            type(name) is not str or name not in ECOLOGY_READ_CALLS | {"doCreateRequest_blocked"} or type(count) is not int or count < 0
            for calls in (
                cast(dict[str, object], write_boundary["calls_at_start"]),
                cast(dict[str, object], write_boundary["calls_at_end"]),
            )
            for name, count in calls.items()
        )
        or set(cast(dict[str, object], write_boundary["read_call_deltas"])) != set(ECOLOGY_READ_CALLS)
        or any(type(value) is not int or value < 1 for value in cast(dict[str, object], write_boundary["read_call_deltas"]).values())
        or any(
            cast(dict[str, int], write_boundary["calls_at_end"]).get(name, 0) - cast(dict[str, int], write_boundary["calls_at_start"]).get(name, 0)
            != cast(dict[str, int], write_boundary["read_call_deltas"])[name]
            for name in ECOLOGY_READ_CALLS
        )
        or not _valid_key_fingerprint_evidence(key_fingerprints)
        or not isinstance(artifact_sha256, dict)
        or set(artifact_sha256)
        != {
            "api-stage-a.yaml",
            "api-stage-b.yaml",
            "jwks-public-keys.json",
            "mcp-grants.json",
            "tool-policies.json",
        }
        or not all(_is_sha256(value) for value in artifact_sha256.values())
        or not isinstance(certificates, dict)
        or set(certificates) != {"ca_sha256", "issuer_sha256", "gateway_sha256"}
        or not all(_is_sha256(value) for value in certificates.values())
        or not isinstance(fixture, dict)
        or set(fixture)
        != {
            "name",
            "write_enabled",
            "leave_contract_version",
            "repository_head_sha",
            "repository_state_sha256",
            "simulator_source_sha256",
            "contract_snapshot_sha256",
        }
        or fixture.get("name") != "ofmcp-ecology-mock"
        or fixture.get("write_enabled") is not False
        or type(fixture.get("leave_contract_version")) is not str
        or not re.fullmatch(r"[0-9a-f]{40}", str(fixture.get("repository_head_sha")))
        or fixture.get("repository_head_sha") != cast(dict[str, Any], repositories)["ofmcp"]["head_sha"]
        or fixture.get("repository_state_sha256") != cast(dict[str, Any], repositories)["ofmcp"]["worktree_sha256"]
        or any(
            not _is_sha256(fixture.get(name))
            for name in (
                "repository_state_sha256",
                "simulator_source_sha256",
                "contract_snapshot_sha256",
            )
        )
    ):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "evidence_provenance")

    mock_run_start = {
        "pid": write_boundary["pid"],
        "create_time": write_boundary["create_time"],
        "do_create_request_blocked": write_boundary["blocked_at_start"],
        "calls": write_boundary["calls_at_start"],
    }
    mock_run_end = {
        "pid": write_boundary["pid"],
        "create_time": write_boundary["create_time"],
        "do_create_request_blocked": write_boundary["blocked_at_end"],
        "calls": write_boundary["calls_at_end"],
    }
    if not _valid_waiting_restart_summary(
        waiting_restart,
        runtime_segments=cast(
            list[dict[str, object]],
            runtime_boundary["segments"],
        ),
        expected_artifact_sha256=cast(Mapping[str, object], artifact_sha256),
        runtime_started=cast(Mapping[str, object], runtime_boundary["started"]),
        runtime_completed=cast(Mapping[str, object], runtime_boundary["completed"]),
        run_started_at=started_at,
        run_completed_at=completed_at,
        mock_run_start=mock_run_start,
        mock_run_end=mock_run_end,
    ):
        raise OperatorError(
            ExitCode.ARTIFACT_INVALID,
            "evidence_waiting_restart",
        )

    try:
        replayed_segments = _validated_runtime_segments(
            initial=cast(Mapping[str, object], runtime_boundary["started"]),
            completed=cast(Mapping[str, object], runtime_boundary["completed"]),
            events=[
                {
                    "event": segment["event"],
                    "at": segment["at"],
                    "record": {
                        "name": segment["process"],
                        **cast(dict[str, object], segment["record"]),
                    },
                }
                for segment in cast(list[dict[str, Any]], runtime_boundary["segments"])
            ],
        )
    except OperatorError as exc:
        raise OperatorError(
            ExitCode.ARTIFACT_INVALID,
            "evidence_runtime_segments",
        ) from exc
    if replayed_segments != runtime_boundary["segments"]:
        raise OperatorError(
            ExitCode.ARTIFACT_INVALID,
            "evidence_runtime_segments",
        )

    raw_checks = checks.get("checks")
    if (
        set(checks) != {"schema", "version", "checks"}
        or checks.get("schema") != f"{SCHEMA}/evidence-checks"
        or checks.get("version") != VERSION
        or type(raw_checks) is not list
        or len(raw_checks) != len(LIVE_EVIDENCE_CHECK_IDS)
        or {item.get("check_id") for item in raw_checks if type(item) is dict} != LIVE_EVIDENCE_CHECK_IDS
        or any(type(item) is not dict or set(item) != {"check_id", "status", "detail"} or item.get("status") != "pass" or item.get("detail") != "ok" for item in raw_checks)
    ):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "evidence_checks")

    log_processes = logs.get("processes")
    expected_log_processes = {
        name: {
            "state": "running",
            "pid": item["pid"],
            "repo_head_sha": item["repo_head_sha"],
            "repo_dirty": item["repo_dirty"],
            "repo_state_sha256": item["repo_state_sha256"],
        }
        for name, item in cast(dict[str, dict[str, object]], runtime).items()
    }
    if (
        set(logs) != {"schema", "version", "processes", "delegated_trace_ids"}
        or logs.get("schema") != f"{SCHEMA}/safe-log-projection"
        or logs.get("version") != VERSION
        or logs.get("delegated_trace_ids") != trace_ids
        or not isinstance(log_processes, dict)
        or log_processes != expected_log_processes
        or _has_forbidden_evidence_key(logs)
    ):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "evidence_logs")

    timeline: list[dict[str, Any]] = []
    for raw_line in (path / "timeline.ndjson").read_bytes().splitlines():
        try:
            item = json.loads(raw_line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "evidence_timeline") from exc
        if type(item) is not dict:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "evidence_timeline")
        timeline.append(cast(dict[str, Any], item))
    segments = cast(list[dict[str, Any]], runtime_boundary["segments"])
    if (
        len(timeline) != len(segments) + 3
        or timeline[0] != {"event": "live_probe_started", "at": run["started_at"], "run_id": run["run_id"]}
        or timeline[-2] != {"event": "interaction_completed", "at": run["completed_at"], "run_id": run["run_id"]}
        or set(timeline[-1]) != {"event", "at", "run_id"}
        or timeline[-1].get("event") != "evidence_sealed"
        or timeline[-1].get("run_id") != run["run_id"]
        or (sealed_at := _parse_evidence_time(timeline[-1].get("at"))) is None
        or sealed_at < completed_at
        or any(
            timeline[index + 1]
            != {
                "event": f"runtime_process_{segment['event']}",
                "at": segment["at"],
                "process": segment["process"],
                "run_id": run["run_id"],
            }
            for index, segment in enumerate(segments)
        )
    ):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "evidence_timeline")
    return {"status": "verified", "run_id": run.get("run_id"), "file_count": len(expected)}


def _evidence_matches_current_deployment(
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
    evidence_path: Path,
) -> bool:
    run = _read_json(evidence_path / "run.json")
    try:
        static = _probe_static_boundary(layout, manifest, candidate)
        return (
            run.get("candidate_ref") == candidate.ref
            and all(run.get(name) == value for name, value in static.items())
            and run.get("runtime_implementations") == _runtime_repository_evidence(layout, manifest, candidate)
        )
    except OperatorError:
        return False


def probe(
    root: Path,
    *,
    case: str,
    restart_waiting_runtime: bool = False,
) -> dict[str, object]:
    layout, _ = _load_deployment(root)
    with _operation_lock(layout):
        return _probe_unlocked(
            root,
            case=case,
            restart_waiting_runtime=restart_waiting_runtime,
        )


def _begin_live_probe(
    layout: Layout,
    manifest: Mapping[str, Any],
    candidate: Candidate,
    *,
    restarted: bool = False,
) -> dict[str, object]:
    policy = _read_json(layout.artifacts / "tool-policies.json")
    policy_revision = policy.get("policy_revision")
    if type(policy_revision) is not str or len(policy_revision) != 64:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "policy_revision")
    mock_boundary = _mock_run_boundary(layout, manifest)
    if mock_boundary.get("do_create_request_blocked") != 0:
        raise OperatorError(ExitCode.LIVE_EVIDENCE_PENDING, "mock_write_counter_nonzero")
    static_boundary = _probe_static_boundary(layout, manifest, candidate)
    runtime_start = _runtime_repository_evidence(layout, manifest, candidate)
    _validate_runtime_repository_binding(runtime_start, static_boundary)
    state = {
        "schema": f"{SCHEMA}/live-probe",
        "version": VERSION,
        "status": "awaiting_feishu",
        "started_at": _now(),
        "candidate_ref": candidate.ref,
        "policy_revision": policy_revision,
        "a6_recorded_seq_baseline": _a6_audit_high_water(layout),
        "mock_boundary": mock_boundary,
        "static_boundary": static_boundary,
        "runtime_start": runtime_start,
        "process_event_offset": _private_file_offset(
            layout.run / "process-events.ndjson",
        ),
        "api_log_offset": _private_file_offset(layout.logs / "api.log"),
    }
    _write_json(layout.run / "live-probe.json", state)
    return {
        "status": "awaiting_feishu",
        "prompt": FIXED_LIVE_PROMPT,
        "instruction": (
            "The runtime boundary changed; send the prompt again, wait for "
            "the seven-field form, run this probe with "
            "--restart-waiting-runtime before submitting it, then run the "
            "probe normally after submission."
            if restarted
            else "Send the prompt to the selected Feishu bot, wait for the "
            "seven-field form, run this probe with "
            "--restart-waiting-runtime before submitting it, then run the "
            "probe normally after submission."
        ),
    }


def _probe_unlocked(
    root: Path,
    *,
    case: str,
    restart_waiting_runtime: bool = False,
) -> dict[str, object]:
    if case != "leave-preview":
        raise OperatorError(ExitCode.ARGUMENT_INVALID, "probe_case")
    layout, manifest = _load_deployment(root)
    probe_path = layout.run / "live-probe.json"
    state: dict[str, Any] | None = None
    if probe_path.exists():
        state = _read_json(probe_path)
        if state.get("schema") != f"{SCHEMA}/live-probe":
            raise OperatorError(ExitCode.AUTHORITY_STALE, "live_probe")
    pending_restart = state.get("waiting_restart") if state is not None else None
    restart_needs_recovery = isinstance(pending_restart, Mapping) and pending_restart.get("status") in {"begin", "failed"}
    try:
        candidate = _selected_candidate(layout)
    except OperatorError:
        if restart_needs_recovery:
            # Candidate authority cannot be reconstructed, so the only safe
            # automated action is to disable any exact same-repo API process.
            ProcessManager(layout).stop(_inventory_specs(manifest)["api"])
            raise OperatorError(
                ExitCode.LIVE_EVIDENCE_PENDING,
                "waiting_restart_recovery_authority",
            ) from None
        raise
    if state is not None and state.get("candidate_ref") != candidate.ref:
        if restart_needs_recovery:
            ProcessManager(layout).stop(_inventory_specs(manifest)["api"])
        raise OperatorError(ExitCode.AUTHORITY_STALE, "live_probe")
    if restart_needs_recovery:
        _reconcile_failed_waiting_restart(layout, manifest, candidate)
        _archive_abandoned_live_probe(
            layout,
            cast(Mapping[str, object], state),
            reason="waiting_restart_recovered_stage_a",
        )
        probe_path.unlink()
        raise OperatorError(
            ExitCode.LIVE_EVIDENCE_PENDING,
            "waiting_restart_recovered_run_up",
        )
    if doctor(root, online=True)["status"] != "ready":
        raise OperatorError(ExitCode.PROCESS_FAILED, "online_doctor")
    if not probe_path.exists():
        if restart_waiting_runtime:
            raise OperatorError(
                ExitCode.LIVE_EVIDENCE_PENDING,
                "live_probe_missing",
            )
        return _begin_live_probe(layout, manifest, candidate)
    if state is None:
        state = _read_json(probe_path)
    if state.get("status") == "passed":
        if restart_waiting_runtime:
            raise OperatorError(
                ExitCode.ARGUMENT_INVALID,
                "live_probe_complete",
            )
        evidence_path = Path(cast(str, state["evidence_path"]))
        verify_evidence(evidence_path)
        if _evidence_matches_current_deployment(
            layout,
            manifest,
            candidate,
            evidence_path,
        ):
            return {"status": "passed", "evidence_path": str(evidence_path)}
        # Keep the immutable old evidence bundle, but establish a fresh
        # temporal/audit/mock boundary for this new deployment lifecycle.
        probe_path.unlink()
        return _probe_unlocked(root, case=case)
    raw_pending_boundary = state.get("mock_boundary")
    if not isinstance(raw_pending_boundary, dict):
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "live_probe_boundary")
    current_mock_boundary = _mock_run_boundary(layout, manifest)
    if raw_pending_boundary.get("pid") != current_mock_boundary.get("pid") or raw_pending_boundary.get("create_time") != current_mock_boundary.get("create_time"):
        _archive_abandoned_live_probe(
            layout,
            state,
            reason="mock_runtime_restarted",
        )
        probe_path.unlink()
        return _begin_live_probe(
            layout,
            manifest,
            candidate,
            restarted=True,
        )
    try:
        started_at = datetime.fromisoformat(cast(str, state["started_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise OperatorError(ExitCode.ARTIFACT_INVALID, "live_probe_time") from exc
    if restart_waiting_runtime:
        raw_static_boundary = state.get("static_boundary")
        if not isinstance(raw_static_boundary, dict) or _probe_static_boundary(layout, manifest, candidate) != raw_static_boundary:
            raise OperatorError(
                ExitCode.AUTHORITY_STALE,
                "static_boundary_changed",
            )
        asyncio.run(
            _validate_waiting_interaction(
                layout,
                manifest,
                candidate,
                started_at=started_at,
            ),
        )
        previous_restart = state.get("waiting_restart")
        if isinstance(previous_restart, Mapping) and previous_restart.get("status") == "complete":
            return {
                "status": "awaiting_feishu",
                "prompt": FIXED_LIVE_PROMPT,
                "instruction": ("The waiting-input restart is already verified; submit the existing form, then run this probe normally."),
            }
        restart_started_at = _now()
        event_offset = _private_file_offset(
            layout.run / "process-events.ndjson",
        )
        state["waiting_restart"] = {
            "status": "begin",
            "started_at": restart_started_at,
        }
        _write_json(probe_path, state)
        try:
            outcome = _restart_waiting_runtime(
                root,
                layout,
                manifest,
                candidate,
            )
            restart_events, _ = _read_json_lines_since(
                layout.run / "process-events.ndjson",
                offset=event_offset,
            )
            restart_segments = _validated_runtime_segments(
                initial=outcome.runtime_before,
                completed=outcome.runtime_after,
                events=restart_events,
            )
            stage_a_overlay_sha256 = _sha256_file(
                layout.artifacts / "api-stage-a.yaml",
            )
            stage_b_overlay_sha256 = _sha256_file(
                layout.artifacts / "api-stage-b.yaml",
            )
            _validate_waiting_restart_segments(
                restart_segments,
                stage_a_overlay_sha256=stage_a_overlay_sha256,
                stage_b_overlay_sha256=stage_b_overlay_sha256,
            )
        except BaseException as exc:
            if isinstance(exc, OperatorError):
                failed_stage = exc.detail
            elif isinstance(exc, KeyboardInterrupt | SystemExit):
                failed_stage = "interrupted"
            else:
                failed_stage = "unexpected"
            state["waiting_restart"] = {
                "status": "failed",
                "started_at": restart_started_at,
                "failed_stage": failed_stage,
            }
            try:
                _write_json(probe_path, state)
            except BaseException:
                # The durable begin marker remains. The next invocation treats
                # both begin and failed identically and performs reconciliation.
                pass
            raise
        restart_completed_at = _now()
        state["waiting_restart"] = {
            "status": "complete",
            "started_at": restart_started_at,
            "completed_at": restart_completed_at,
            "stage_a_overlay_sha256": stage_a_overlay_sha256,
            "stage_b_overlay_sha256": stage_b_overlay_sha256,
            "nonrestartable_before_sha256": _nonrestartable_runtime_sha256(
                outcome.runtime_before,
            ),
            "nonrestartable_after_sha256": _nonrestartable_runtime_sha256(
                outcome.runtime_after,
            ),
            "mock_before": dict(outcome.mock_before),
            "mock_after": dict(outcome.mock_after),
            "segments": restart_segments,
        }
        _write_json(probe_path, state)
        return dict(outcome.response)
    try:
        raw_static_boundary = state.get("static_boundary")
        raw_runtime_start = state.get("runtime_start")
        process_event_offset = state.get("process_event_offset")
        api_log_offset = state.get("api_log_offset")
        if not isinstance(raw_static_boundary, dict) or not isinstance(raw_runtime_start, dict) or type(process_event_offset) is not int or type(api_log_offset) is not int:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "live_probe_provenance")
        if _probe_static_boundary(layout, manifest, candidate) != raw_static_boundary:
            raise OperatorError(ExitCode.AUTHORITY_STALE, "static_boundary_changed")
        runtime_end = _runtime_repository_evidence(layout, manifest, candidate)
        process_events, process_event_end_offset = _read_json_lines_since(
            layout.run / "process-events.ndjson",
            offset=process_event_offset,
        )
        runtime_segments = _validated_runtime_segments(
            initial=raw_runtime_start,
            completed=runtime_end,
            events=process_events,
        )
        waiting_restart = state.get("waiting_restart")
        if not _valid_waiting_restart_summary(
            waiting_restart,
            runtime_segments=runtime_segments,
            expected_artifact_sha256=cast(
                Mapping[str, object],
                raw_static_boundary["artifact_sha256"],
            ),
            runtime_started=raw_runtime_start,
            runtime_completed=runtime_end,
            run_started_at=started_at,
            run_completed_at=datetime.now(UTC),
            mock_run_start=raw_pending_boundary,
            mock_run_end=current_mock_boundary,
        ):
            raise OperatorError(
                ExitCode.LIVE_EVIDENCE_PENDING,
                "waiting_restart_required",
            )
        _validate_runtime_repository_binding(raw_runtime_start, raw_static_boundary)
        _validate_runtime_repository_binding(runtime_end, raw_static_boundary)
        for segment in runtime_segments:
            _validate_runtime_repository_binding(
                {cast(str, segment["process"]): segment["record"]},
                raw_static_boundary,
            )
        live_checks, completed_at = asyncio.run(
            _live_database_checks(
                layout,
                manifest,
                candidate,
                started_at=started_at,
            ),
        )
        raw_mock_boundary = state.get("mock_boundary")
        baseline_seq = state.get("a6_recorded_seq_baseline")
        policy_revision = state.get("policy_revision")
        if not isinstance(raw_mock_boundary, dict) or type(baseline_seq) is not int or baseline_seq < 0 or type(policy_revision) is not str:
            raise OperatorError(ExitCode.ARTIFACT_INVALID, "live_probe_boundary")
        mock_boundary_end = _mock_run_boundary(
            layout,
            manifest,
            expected=raw_mock_boundary,
        )
        audit_end_seq = _a6_audit_high_water(layout)
        audit_correlation = _a6_audit_correlation(
            layout,
            manifest,
            baseline_seq=baseline_seq,
            end_seq=audit_end_seq,
            policy_revision=policy_revision,
        )
        trace_ids, _api_log_end_offset = _delegated_trace_ids_since(
            layout,
            offset=api_log_offset,
        )
        if trace_ids != audit_correlation.get("trace_ids"):
            raise OperatorError(
                ExitCode.LIVE_EVIDENCE_PENDING,
                "cross_repository_trace",
            )
        # Seal-time revalidation. Controlled API/supervisor restarts are
        # accepted only through the append-only lifecycle chain; all static
        # authority and non-restartable process records stay byte-for-byte bound.
        if _probe_static_boundary(layout, manifest, candidate) != raw_static_boundary:
            raise OperatorError(ExitCode.AUTHORITY_STALE, "static_boundary_changed")
        runtime_end = _runtime_repository_evidence(layout, manifest, candidate)
        process_events, process_event_end_offset = _read_json_lines_since(
            layout.run / "process-events.ndjson",
            offset=process_event_offset,
        )
        runtime_segments = _validated_runtime_segments(
            initial=raw_runtime_start,
            completed=runtime_end,
            events=process_events,
        )
        if not _valid_waiting_restart_summary(
            waiting_restart,
            runtime_segments=runtime_segments,
            expected_artifact_sha256=cast(
                Mapping[str, object],
                raw_static_boundary["artifact_sha256"],
            ),
            runtime_started=raw_runtime_start,
            runtime_completed=runtime_end,
            run_started_at=started_at,
            run_completed_at=completed_at,
            mock_run_start=raw_mock_boundary,
            mock_run_end=mock_boundary_end,
        ):
            raise OperatorError(
                ExitCode.LIVE_EVIDENCE_PENDING,
                "waiting_restart_changed",
            )
        _validate_runtime_repository_binding(runtime_end, raw_static_boundary)
        for segment in runtime_segments:
            _validate_runtime_repository_binding(
                {cast(str, segment["process"]): segment["record"]},
                raw_static_boundary,
            )
    except OperatorError as exc:
        if exc.code is ExitCode.LIVE_EVIDENCE_PENDING:
            return {
                "status": "awaiting_feishu",
                "prompt": FIXED_LIVE_PROMPT,
                "check": exc.detail,
            }
        raise
    evidence_path = _write_evidence(
        layout,
        manifest,
        candidate,
        started_at=started_at,
        completed_at=completed_at,
        audit_correlation=audit_correlation,
        mock_boundary_start=raw_mock_boundary,
        mock_boundary_end=mock_boundary_end,
        static_boundary=raw_static_boundary,
        runtime_start=raw_runtime_start,
        runtime_end=runtime_end,
        runtime_segments=runtime_segments,
        waiting_restart=cast(Mapping[str, object], waiting_restart),
        process_event_end_offset=process_event_end_offset,
        trace_ids=trace_ids,
        live_checks=live_checks,
    )
    verify_evidence(evidence_path)
    state.update(
        {
            "status": "passed",
            "completed_at": completed_at.isoformat(),
            "evidence_path": str(evidence_path),
        },
    )
    _write_json(probe_path, state)
    return {"status": "passed", "evidence_path": str(evidence_path)}


def _add_root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True, type=Path, help="absolute external deployment root")


def _build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(description="Secure Feishu leave-preview local vertical-slice operator")
    commands = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = commands.add_parser("bootstrap", help="create or validate external keys and local PKI")
    _add_root_argument(bootstrap_parser)
    bootstrap_parser.add_argument("--ofmcp-repo", required=True, type=Path)
    bootstrap_parser.add_argument("--api-port", type=int, default=DEFAULT_PORTS["api"])
    bootstrap_parser.add_argument("--gateway-port", type=int, default=DEFAULT_PORTS["gateway"])
    bootstrap_parser.add_argument("--jwks-port", type=int, default=DEFAULT_PORTS["jwks"])
    bootstrap_parser.add_argument("--mock-port", type=int, default=DEFAULT_PORTS["mock"])
    bootstrap_parser.add_argument("--format", choices=("json",), default="json")

    discover_parser = commands.add_parser("discover", help="discover existing eligible authority")
    _add_root_argument(discover_parser)

    prepare_parser = commands.add_parser("prepare", help="build reviewed policy/grant artifacts")
    _add_root_argument(prepare_parser)
    prepare_parser.add_argument("--apply", action="store_true")

    doctor_parser = commands.add_parser("doctor", help="run stable offline and optional online checks")
    _add_root_argument(doctor_parser)
    doctor_parser.add_argument("--online", action="store_true")
    doctor_parser.add_argument("--format", choices=("human", "json"), default="human")

    up_parser = commands.add_parser("up", help="run migrations and two-stage startup")
    _add_root_argument(up_parser)

    status_parser = commands.add_parser("status", help="show process provenance")
    _add_root_argument(status_parser)
    status_parser.add_argument("--format", choices=("human", "json"), default="human")

    probe_parser = commands.add_parser("probe", help="open or verify one real Feishu live probe")
    _add_root_argument(probe_parser)
    probe_parser.add_argument("--case", choices=("leave-preview",), required=True)
    probe_parser.add_argument(
        "--restart-waiting-runtime",
        action="store_true",
        help=("after the form is delivered but before submission, restart only the API and Channel supervisor"),
    )

    down_parser = commands.add_parser("down", help="disable producer and stop owned processes")
    _add_root_argument(down_parser)

    evidence_parser = commands.add_parser("evidence", help="evidence bundle operations")
    evidence_commands = evidence_parser.add_subparsers(dest="evidence_command", required=True)
    verify_parser = evidence_commands.add_parser("verify", help="verify hashes, schema, modes, and redaction")
    verify_parser.add_argument("path", type=Path)
    return parser


def _render_human(result: Mapping[str, object]) -> str:
    checks = result.get("checks")
    if isinstance(checks, list):
        lines = [f"status: {result.get('status', 'unknown')}"]
        for item in checks:
            if isinstance(item, dict):
                lines.append(f"[{item.get('status', 'unknown')}] {item.get('check_id', 'unknown')}: {item.get('detail', '')}")
        return "\n".join(lines)
    processes = result.get("processes")
    if isinstance(processes, dict):
        lines = [f"status: {result.get('status', 'unknown')}"]
        for name, item in sorted(processes.items()):
            state = item.get("state", "unknown") if isinstance(item, dict) else "unknown"
            lines.append(f"{name}: {state}")
        return "\n".join(lines)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _execute_cli(args: argparse.Namespace) -> tuple[dict[str, object], str]:
    command = cast(str, args.command)
    if command == "bootstrap":
        result = bootstrap(
            args.root,
            ofmcp_repo=args.ofmcp_repo,
            ports={
                "api": args.api_port,
                "gateway": args.gateway_port,
                "jwks": args.jwks_port,
                "mock": args.mock_port,
            },
        )
        return result, "json"
    if command == "discover":
        return discover(args.root), "json"
    if command == "prepare":
        return prepare(args.root, apply=args.apply), "json"
    if command == "doctor":
        return doctor(args.root, online=args.online), cast(str, args.format)
    if command == "up":
        return up(args.root), "json"
    if command == "status":
        return status(args.root), cast(str, args.format)
    if command == "probe":
        return (
            probe(
                args.root,
                case=args.case,
                restart_waiting_runtime=args.restart_waiting_runtime,
            ),
            "json",
        )
    if command == "down":
        return down(args.root), "json"
    if command == "evidence" and args.evidence_command == "verify":
        return verify_evidence(args.path), "json"
    raise OperatorError(ExitCode.ARGUMENT_INVALID)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        result, output_format = _execute_cli(args)
    except OperatorError as exc:
        print(
            json.dumps(
                {
                    "status": "rejected",
                    "code": exc.code.value,
                    "detail": exc.detail,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        return 2
    except KeyboardInterrupt:
        print('{"code":"INTERRUPTED","status":"failed"}')
        return 130
    except Exception:
        print(
            json.dumps(
                {"status": "failed", "code": ExitCode.INTERNAL_ERROR.value},
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        return 1
    print(
        _render_human(result) if output_format == "human" else json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    )
    return 0 if result.get("status") not in {"failed", "awaiting_feishu", "selection_required"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
