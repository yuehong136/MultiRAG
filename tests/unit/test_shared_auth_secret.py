"""Shared auth signing key configuration and race-safe lazy initialization."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import redis
from pydantic import SecretStr

from common import app_config, config_utils, resources
from core.utils.redis_conn import REDIS_CONN

TEST_KEYS = {name: f"test-only-{name}-" + "x" * 48 for name in ["yaml", "local", "external", "env", "legacy", "redis", "candidate"]}


def test_config_sources_and_redaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "configs"
    directory.mkdir()
    monkeypatch.setattr(config_utils, "get_project_base_directory", lambda: str(tmp_path))
    for key in ["MULTIRAG_SECRET_KEY", "MULTIRAG_MULTIRAG__SECRET_KEY", "MULTIRAG_CONFIG_OVERLAY_FILE"]:
        monkeypatch.delenv(key, raising=False)
    (directory / "service_conf.yaml").write_text(f"multirag: {{secret_key: '{TEST_KEYS['yaml']}'}}\n")

    def resolved() -> str:
        cfg = app_config.load_app_config()
        assert cfg.multirag.secret_key is not None
        key = cfg.multirag.secret_key.get_secret_value()
        assert key not in repr(cfg.multirag) and key not in cfg.multirag.model_dump_json()
        return key

    assert resolved() == TEST_KEYS["yaml"]
    (directory / "local.service_conf.yaml").write_text(f"multirag: {{secret_key: '{TEST_KEYS['local']}'}}\n")
    assert resolved() == TEST_KEYS["local"]
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(f"multirag: {{secret_key: '{TEST_KEYS['external']}'}}\n")
    overlay.chmod(0o600)
    monkeypatch.setenv("MULTIRAG_CONFIG_OVERLAY_FILE", str(overlay))
    assert resolved() == TEST_KEYS["external"]
    monkeypatch.setenv("MULTIRAG_SECRET_KEY", "historical-short-placeholder")
    assert resolved() == TEST_KEYS["external"]
    monkeypatch.setenv("MULTIRAG_SECRET_KEY", TEST_KEYS["legacy"])
    assert resolved() == TEST_KEYS["legacy"]
    monkeypatch.setenv("MULTIRAG_MULTIRAG__SECRET_KEY", TEST_KEYS["env"])
    assert resolved() == TEST_KEYS["env"]
    monkeypatch.setenv("MULTIRAG_MULTIRAG__SECRET_KEY", "1" * 40)
    assert resolved() == "1" * 40


class FakeRedis:
    def __init__(self, value: str | None = None, *, lose_race: bool = False, disappearing: bool = False) -> None:
        self.value = value
        self.lose_race = lose_race
        self.disappearing = disappearing
        self.writes = 0

    def get(self, key: str) -> str | None:
        return self.value

    def setnx(self, key: str, value: str) -> bool:
        self.writes += 1
        if self.disappearing:
            return False
        if self.lose_race:
            self.value = TEST_KEYS["redis"]
            return False
        self.value = value
        return True


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("existing or configured key must not generate a candidate")


def test_fixed_key_never_contacts_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources, "get_app_config", lambda: SimpleNamespace(multirag=SimpleNamespace(secret_key=SecretStr(TEST_KEYS["env"]))))
    monkeypatch.setattr(REDIS_CONN, "get_or_create_secret_key", forbidden)
    monkeypatch.setattr(resources.secrets, "token_hex", forbidden)
    assert resources._create_secret_key() == TEST_KEYS["env"]


@pytest.mark.parametrize("configured", [None, "", "historical-short-key"])
def test_fallback_uses_existing_key_without_generation(monkeypatch: pytest.MonkeyPatch, configured: str | None, caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeRedis(TEST_KEYS["redis"])
    monkeypatch.setattr(REDIS_CONN, "REDIS", fake)
    monkeypatch.setattr(resources, "get_app_config", lambda: SimpleNamespace(multirag=SimpleNamespace(secret_key=SecretStr(configured) if configured is not None else None)))
    monkeypatch.setattr(resources.secrets, "token_hex", forbidden)
    assert resources._create_secret_key() == TEST_KEYS["redis"]
    assert fake.writes == 0 and TEST_KEYS["redis"] not in caplog.text


@pytest.mark.parametrize("lose_race", [False, True])
def test_atomic_initialization_always_returns_stored_winner(monkeypatch: pytest.MonkeyPatch, lose_race: bool) -> None:
    fake = FakeRedis(lose_race=lose_race)
    monkeypatch.setattr(REDIS_CONN, "REDIS", fake)
    assert REDIS_CONN.get_or_create_secret_key("owned-test-key", lambda: TEST_KEYS["candidate"]) == (TEST_KEYS["redis"] if lose_race else TEST_KEYS["candidate"])
    assert fake.writes == 1


def test_disappearing_winner_fails_closed_with_bounded_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis(disappearing=True)
    monkeypatch.setattr(REDIS_CONN, "REDIS", fake)
    with pytest.raises(redis.RedisError, match="did not converge"):
        REDIS_CONN.get_or_create_secret_key("owned-test-key", TEST_KEYS["candidate"])
    assert fake.writes == 4


def test_empty_stored_key_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis("")
    monkeypatch.setattr(REDIS_CONN, "REDIS", fake)
    with pytest.raises(redis.RedisError, match="Stored authentication secret is invalid"):
        REDIS_CONN.get_or_create_secret_key("owned-test-key", forbidden)
    assert fake.writes == 0


def test_cold_import_initializes_before_api_session_admin_and_jwt_signers() -> None:
    import json
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-m", "tests.support.shared_secret_probe"], cwd=root, text=True, capture_output=True, timeout=90)
    assert result.returncode == 0, result.stderr[-4000:]
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert evidence["startup_before_all_signers"] is True and evidence["jwt_roundtrip"] is True
    assert Path(evidence["api_root"]).resolve() == root / "api/apps"


@pytest.mark.parametrize("lose_race", [False, True])
def test_only_the_redis_creator_warns_without_disclosing_key(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, lose_race: bool) -> None:
    fake = FakeRedis(lose_race=lose_race)
    monkeypatch.setattr(REDIS_CONN, "REDIS", fake)
    monkeypatch.setattr(resources, "get_app_config", lambda: SimpleNamespace(multirag=SimpleNamespace(secret_key=None)))
    monkeypatch.setattr(resources.secrets, "token_hex", lambda count: TEST_KEYS["candidate"])
    expected = TEST_KEYS["redis"] if lose_race else TEST_KEYS["candidate"]
    assert resources._create_secret_key() == expected
    assert ("Using auto-generated SECRET_KEY" in caplog.text) is not lose_race
    assert expected not in caplog.text
