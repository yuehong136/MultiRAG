"""Real Redis audio reuse and independent SQL usage readback, with owned resources."""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import redis
import sqlalchemy as sa
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from api.db.db_models import TenantLLM
from api.db.services import tenant_llm_service as tenant_models
from api.db.services.dialog_service import tts
from api.db.services.llm_service import LLMBundle
from core.utils import tts_cache as module
from tests.support.database import scratch_database


class ControlledSpeech:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.audio = b"controlled-audio"
        self.fail = False

    def tts(self, text: str) -> Iterator[bytes | int]:
        self.calls.append(text)
        yield self.audio
        if self.fail:
            raise RuntimeError("controlled interrupted speech")
        yield 7


@pytest.fixture
def speech_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    client = module.REDIS_CONN.REDIS
    assert client is not None and client.ping()
    reader = redis.Redis(connection_pool=redis.ConnectionPool(**client.connection_pool.connection_kwargs))
    keys: list[str] = []
    manifest = tmp_path / "redis-keys.json"

    def register(bundle: LLMBundle, text: str) -> str:
        key = module._build_key(bundle, text)
        assert key is not None
        if key not in keys:
            assert not reader.exists(key)
            keys.append(key)
            manifest.write_text(json.dumps({"keys": keys, "absent": False}, indent=2))
        return key

    with scratch_database(tmp_path / "database") as engine:

        @contextmanager
        def connection() -> Iterator[Session]:
            with Session(engine) as db:
                yield db

        monkeypatch.setattr(tenant_models, "db_connection", connection)
        monkeypatch.delenv("RAGFLOW_TTS_CACHE_TTL_SECONDS", raising=False)
        providers: list[ControlledSpeech] = []

        def model_instance(*args: Any, **kwargs: Any) -> ControlledSpeech:
            provider = ControlledSpeech()
            providers.append(provider)
            return provider

        monkeypatch.setattr(tenant_models.TenantLLMService, "model_instance", model_instance)
        tenant_id = uuid4().hex
        with Session(engine) as db:
            row = TenantLLM(tenant_id=tenant_id, llm_factory="Controlled", llm_name="speech", mdl_type="tts", api_key="fixture-only", api_base="http://fixture.invalid", used_tokens=0)
            db.add(row)
            db.commit()
            model_id = row.id

        def bundle() -> LLMBundle:
            with Session(engine) as db:
                config = db.get(TenantLLM, model_id).to_dict()
            return LLMBundle(None, tenant_id, config)

        def usage() -> int:
            with Session(engine) as db:
                return int(db.scalar(sa.select(TenantLLM.used_tokens).where(TenantLLM.id == model_id)))

        try:
            yield {"engine": engine, "bundle": bundle, "usage": usage, "providers": providers, "reader": reader, "register": register}
        finally:
            for key in keys:
                client.delete(key)
                assert not reader.exists(key)
            manifest.write_text(json.dumps({"keys": keys, "absent": True}, indent=2))
            reader.close()
            reader.connection_pool.disconnect()


def test_canvas_dialog_share_redis_and_only_real_synthesis_increments_sql_usage(speech_cache: dict[str, Any]) -> None:
    env = speech_cache
    first = env["bundle"]()
    key = env["register"](first, "Hello world")
    assert Canvas.tts(None, first, " Hello\n\U0001f600 world ") == b"controlled-audio".hex()
    assert env["reader"].get(key) in (b"controlled-audio".hex(), b"controlled-audio".hex().encode())
    assert 0 < env["reader"].ttl(key) <= 604800
    assert env["usage"]() == 7
    # Reload a fresh bundle after usage/timestamps changed in SQL.
    second = env["bundle"]()
    assert module._build_key(second, "Hello world") == key
    assert tts(second, "Hello world") == b"controlled-audio".hex()
    assert env["providers"][0].calls == ["Hello world"]
    assert env["providers"][1].calls == []
    assert env["usage"]() == 7


def test_real_redis_ttl_expires_and_disable_bypasses_existing_audio(speech_cache: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = speech_cache
    bundle = env["bundle"]()
    key = env["register"](bundle, "expire")
    monkeypatch.setenv("RAGFLOW_TTS_CACHE_TTL_SECONDS", "1")
    assert tts(bundle, "expire")
    assert 0 < env["reader"].pttl(key) <= 1000
    time.sleep(1.1)
    assert not env["reader"].exists(key)
    assert tts(bundle, "expire")
    monkeypatch.setenv("RAGFLOW_TTS_CACHE_TTL_SECONDS", "0")
    assert tts(bundle, "expire")
    assert len(env["providers"][0].calls) == 3
    assert env["usage"]() == 21


def test_error_and_empty_audio_leave_no_redis_entry(speech_cache: dict[str, Any]) -> None:
    env = speech_cache
    bundle = env["bundle"]()
    provider = env["providers"][0]
    provider.fail = True
    key = env["register"](bundle, "retry")
    assert tts(bundle, "retry") is None
    assert not env["reader"].exists(key)
    assert env["usage"]() == 0
    provider.fail = False
    provider.audio = b""
    assert tts(bundle, "retry") is None
    assert not env["reader"].exists(key)
    # A provider may report usage for an empty successful response; the cache
    # must preserve bundle accounting, while refusing to store empty audio.
    assert env["usage"]() == 7
    provider.audio = b"retried"
    assert tts(bundle, "retry") == b"retried".hex()
    assert env["reader"].get(key) in (b"retried".hex(), b"retried".hex().encode())
    assert env["usage"]() == 14


def test_real_redis_separates_tenant_model_and_voice_config(speech_cache: dict[str, Any]) -> None:
    env = speech_cache
    bundles = [env["bundle"]() for _ in range(4)]
    bundles[1].tenant_id = uuid4().hex
    bundles[2].model_config["llm_name"] = "different-model"
    bundles[3].model_config["api_key_payload"] = {"reference_id": "different-voice"}
    for position, bundle in enumerate(bundles):
        env["providers"][position].audio = f"audio-{position}".encode()
        key = env["register"](bundle, "same text")
        assert tts(bundle, "same text") == f"audio-{position}".encode().hex()
        assert env["reader"].exists(key)
    assert all(len(provider.calls) == 1 for provider in env["providers"])
