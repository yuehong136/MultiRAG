"""The compose wiring that decides whether channels work, and who holds the key.

Parses ``docker-compose.yml`` directly instead of shelling out to
``docker compose config``: the binary is not present on every machine that runs
this suite, and the properties asserted here are static text, not the result of
interpolation.
"""

from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT_DIR / "docker" / "docker-compose.yml"

_MASTER_KEY_VAR = "MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY"
_TOKEN_VAR = "MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN"
_SUPERVISOR = "multirag-channel-supervisor"
_API_SERVICES = ("multirag-cpu", "multirag-gpu")


def _services() -> dict[str, Any]:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return dict(compose["services"])


def _environment(service: dict[str, Any]) -> dict[str, str]:
    """Compose accepts a list or a mapping; this file uses the list form."""

    raw = service.get("environment", [])
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    entries = {}
    for item in raw:
        name, _, value = str(item).partition("=")
        entries[name] = value
    return entries


def test_compose_ships_a_channel_supervisor_service() -> None:
    """Without this service the whole subsystem is silently inert.

    The management page still creates channels, saves them and accepts an
    enable click; the runtime just stays at ``waiting`` forever -- and that
    ``waiting`` is byte-identical to "starting up", so nothing in the UI says
    anything is wrong.
    """

    services = _services()
    assert _SUPERVISOR in services

    supervisor = services[_SUPERVISOR]
    # Opt-in: a deployment that does not ask for channels must not get a
    # container that crash-loops on missing configuration.
    assert supervisor["profiles"] == ["channel"]
    assert supervisor["entrypoint"] == ["python", "-m", "api.channels.supervisor"]
    # The supervisor forks worker children; PID 1 has to reap them.
    assert supervisor["init"] is True
    # Long enough for every worker child to stop gracefully.
    assert supervisor["stop_grace_period"] == "40s"


def test_only_the_api_service_receives_the_credential_master_key() -> None:
    services = _services()
    supervisor_env = _environment(services[_SUPERVISOR])

    # Empty, and empty *explicitly*: `environment` outranks `env_file`, so this
    # line survives an operator putting the key in .env. That turns "only the
    # API holds the master key" from a convention in a README into one
    # greppable line. Decryption happens on the API side; a runner only ever
    # receives per-binding connection material through the private route.
    assert supervisor_env[_MASTER_KEY_VAR] == ""
    assert supervisor_env["CHANNEL_SECRET_ENCRYPTION_KEY"] == ""

    # It does still need the token -- that is what authenticates it to the
    # private routes -- and a base URL to reach them.
    assert supervisor_env[_TOKEN_VAR] != ""
    assert supervisor_env["MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL"] != ""

    for name in _API_SERVICES:
        api_env = _environment(services[name])
        assert api_env[_MASTER_KEY_VAR] == "${CHANNEL_SECRET_ENCRYPTION_KEY:-}"
        assert api_env[_TOKEN_VAR] == "${CHANNEL_INTERNAL_API_TOKEN:-}"


def test_the_supervisor_does_not_share_the_api_config_directory() -> None:
    """``../configs`` is where the API renders its config, master key included.

    Mounting it here would hand over the key through the filesystem and undo
    the environment separation above. Everything the worker children need
    instead arrives as explicit overrides, because the config baked into the
    image points Redis at 127.0.0.1, which means nothing inside a container.
    """

    supervisor = _services()[_SUPERVISOR]
    mounts = [str(volume) for volume in supervisor.get("volumes", [])]
    assert not [mount for mount in mounts if mount.startswith("../configs")]

    env = _environment(supervisor)
    assert env["MULTIRAG_REDIS__HOST"] == "${REDIS_HOST:-redis}:6379"
    for variable in ("MULTIRAG_REDIS__DB", "MULTIRAG_REDIS__USERNAME", "MULTIRAG_REDIS__PASSWORD"):
        assert variable in env


def test_the_supervisor_declares_no_cross_profile_dependency() -> None:
    """It would have to depend on cpu *or* gpu, and compose rejects the loser.

    An API that is not up yet only costs one skipped reconcile tick, which the
    next one recovers; missing configuration exits non-zero into docker's
    restart backoff. Neither needs `depends_on`.
    """

    assert "depends_on" not in _services()[_SUPERVISOR]
