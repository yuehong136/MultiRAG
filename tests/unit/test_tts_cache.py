"""Shared speech cache isolation, cost semantics and failure boundaries."""

from collections.abc import Iterator
from copy import deepcopy
from typing import Any
from unittest.mock import Mock

import pytest

from agent.canvas import Canvas
from api.db.services.dialog_service import tts
from core.utils import tts_cache as module


class Speech:
    def __init__(self) -> None:
        self.tenant_id = "tenant-one"
        self.model_config: dict[str, Any] = {"id": 1, "llm_factory": "Controlled", "llm_name": "speech", "api_key": {"voice": "alice"}, "api_base": "http://speech.invalid"}
        self.calls: list[str] = []
        self.chunks: list[Any] = [b"audio", bytearray(b"-end")]
        self.error: Exception | None = None

    def tts(self, text: str) -> Iterator[bytes]:
        self.calls.append(text)
        yield from self.chunks
        if self.error:
            raise self.error


@pytest.fixture
def cache(monkeypatch: pytest.MonkeyPatch) -> Mock:
    values: dict[str, str] = {}
    client = Mock()
    client.get.side_effect = values.get

    def store(key: str, value: str, exp: int) -> bool:
        values[key] = value
        return True

    client.set.side_effect = store
    monkeypatch.setattr(module, "REDIS_CONN", client)
    monkeypatch.delenv("RAGFLOW_TTS_CACHE_TTL_SECONDS", raising=False)
    return client


def test_both_entry_points_share_cleaned_audio_without_a_second_call(cache: Mock) -> None:
    speech = Speech()
    expected = b"audio-end".hex()
    assert Canvas.tts(None, speech, "  Hello\x00\n\U0001f600 world  ") == expected
    assert tts(speech, "Hello world") == expected
    assert speech.calls == ["Hello world"]
    assert cache.set.call_args.kwargs == {"exp": 604800}
    assert cache.set.call_count == 1


@pytest.mark.parametrize("entry", [tts, lambda speech, text: Canvas.tts(None, speech, text)])
def test_cleaned_empty_input_has_no_io(cache: Mock, entry: Any) -> None:
    speech = Speech()
    assert entry(speech, "\x00\U0001f600\n ") is None
    assert entry(None, "hello") is None
    assert speech.calls == []
    cache.get.assert_not_called()
    cache.set.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", 2), ("llm_name", "other"), ("llm_factory", "Other"), ("api_base", "http://other.invalid"), ("api_key", {"voice": "bob"}), ("voice", "bob"), ("speed", 2)],
)
def test_model_and_voice_configuration_changes_do_not_share_audio(cache: Mock, field: str, value: Any) -> None:
    first, second = Speech(), Speech()
    second.model_config[field] = value
    assert tts(first, "hello")
    assert tts(second, "hello")
    assert first.calls == second.calls == ["hello"]
    assert cache.set.call_count == 2


def test_tenants_are_separate_and_usage_does_not_invalidate_audio(cache: Mock) -> None:
    first, second = Speech(), Speech()
    second.tenant_id = "tenant-two"
    assert tts(first, "hello") and tts(second, "hello")
    first.model_config.update(used_tokens=100, update_time=100)
    assert tts(first, "hello")
    assert first.calls == second.calls == ["hello"]
    keys = [call.args[0] for call in cache.set.call_args_list]
    assert len(set(keys)) == 2
    assert all("tenant" not in key and "alice" not in key and "hello" not in key for key in keys)


@pytest.mark.parametrize("ttl", ["0", "-5"])
def test_disabled_cache_does_not_even_read_existing_audio(cache: Mock, monkeypatch: pytest.MonkeyPatch, ttl: str) -> None:
    speech = Speech()
    assert tts(speech, "hello")
    cache.reset_mock()
    monkeypatch.setenv("RAGFLOW_TTS_CACHE_TTL_SECONDS", ttl)
    assert tts(speech, "hello") and tts(speech, "hello")
    assert len(speech.calls) == 3
    cache.get.assert_not_called()
    cache.set.assert_not_called()


@pytest.mark.parametrize(("ttl", "expected"), [("17", 17), ("invalid", 604800), ("", 604800)])
def test_configured_and_invalid_ttl(cache: Mock, monkeypatch: pytest.MonkeyPatch, ttl: str, expected: int) -> None:
    monkeypatch.setenv("RAGFLOW_TTS_CACHE_TTL_SECONDS", ttl)
    assert tts(Speech(), "hello")
    assert cache.set.call_args.kwargs == {"exp": expected}


@pytest.mark.parametrize("cached", [b"not hex", "a", "", b"\xff", 12, "aa bb"])
def test_invalid_cache_values_are_replaced_after_synthesis(cache: Mock, cached: Any) -> None:
    cache.get.side_effect = None
    cache.get.return_value = cached
    speech = Speech()
    assert tts(speech, "hello") == b"audio-end".hex()
    assert speech.calls == ["hello"]
    cache.set.assert_called_once()


def test_cached_bytes_are_accepted_without_provider_or_store(cache: Mock) -> None:
    cache.get.side_effect = None
    cache.get.return_value = b"0102aaff"
    speech = Speech()
    assert tts(speech, "hello") == "0102aaff"
    assert speech.calls == []
    cache.set.assert_not_called()


@pytest.mark.parametrize("operation", ["get", "set"])
def test_redis_failure_preserves_synthesized_audio(cache: Mock, operation: str) -> None:
    getattr(cache, operation).side_effect = ConnectionError("controlled Redis failure")
    speech = Speech()
    assert tts(speech, "hello") == b"audio-end".hex()
    assert speech.calls == ["hello"]


@pytest.mark.parametrize("chunks", [[], [b""], [b"partial", "provider error"], [b"partial", 3]])
def test_empty_or_invalid_audio_is_never_cached(cache: Mock, chunks: list[Any]) -> None:
    speech = Speech()
    speech.chunks = chunks
    assert tts(speech, "hello") is None
    cache.set.assert_not_called()


def test_partial_provider_failure_is_not_cached_or_logged_as_text(cache: Mock, caplog: pytest.LogCaptureFixture) -> None:
    speech = Speech()
    speech.error = RuntimeError("private-speech-and-credential")
    assert tts(speech, "private-speech-and-credential") is None
    assert "private-speech-and-credential" not in caplog.text
    cache.set.assert_not_called()
    speech.error = None
    assert tts(speech, "private-speech-and-credential")
    assert len(speech.calls) == 2


@pytest.mark.parametrize("missing", ["tenant", "model", "config", "unsupported-config"])
def test_missing_or_unserializable_identity_bypasses_cache(cache: Mock, missing: str) -> None:
    speech = Speech()
    if missing == "tenant":
        speech.tenant_id = ""
    elif missing == "model":
        speech.model_config = {}
    elif missing == "config":
        del speech.model_config
    else:
        speech.model_config["unknown"] = object()
    assert tts(speech, "hello")
    cache.get.assert_not_called()
    cache.set.assert_not_called()


def test_key_is_deterministic_and_does_not_mutate_config() -> None:
    speech = Speech()
    before = deepcopy(speech.model_config)
    key = module._build_key(speech, "hello")
    speech.model_config = dict(reversed(list(speech.model_config.items())))
    assert module._build_key(speech, "hello") == key
    assert module._build_key(speech, "different") != key
    assert speech.model_config == before
