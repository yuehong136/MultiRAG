"""Discovered Fish voice identities must reach the actual TTS request."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import ormsgpack
import pytest

from core.llm.tts import FishAudioTTS

VOICE_ID = "0123456789abcdef01234567"
OTHER_VOICE_ID = "fedcba9876543210fedcba98"


@pytest.mark.parametrize(
    "key",
    [
        "fixture-token",
        json.dumps({"fish_audio_ak": "fixture-token"}),
        json.dumps({"access_token": "fixture-token"}),
        json.dumps({"api_key": "fixture-token"}),
        json.dumps({"fish_audio_ak": "fixture-token", "fish_audio_refid": VOICE_ID}),
    ],
)
def test_discovered_voice_drives_msgpack_reference(key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, Any]] = []
    closed: list[bool] = []

    class Client:
        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: Any) -> None:
            closed.append(True)

        @contextmanager
        def stream(self, **kwargs: Any) -> Iterator[httpx.Response]:
            requests.append(kwargs)
            response = httpx.Response(200, content=b"voice-audio")
            try:
                yield response
            finally:
                response.close()

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr("core.llm.tts.num_tokens_from_string", lambda _text: 0)
    model = FishAudioTTS(key, VOICE_ID)
    assert list(model.tts("hello")) == [b"voice-audio", 0]
    assert len(requests) == 1
    request = requests[0]
    assert request["url"] == "https://api.fish.audio/v1/tts"
    assert request["headers"] == {"api-key": "fixture-token", "content-type": "application/msgpack"}
    assert ormsgpack.unpackb(request["content"])["reference_id"] == VOICE_ID
    assert closed == [True]


def test_legacy_name_preserves_explicit_reference() -> None:
    model = FishAudioTTS(json.dumps({"fish_audio_ak": "fixture-token", "fish_audio_refid": "legacy-reference"}), "legacy-model")
    assert model.ref_id == "legacy-reference"


def test_selected_voice_rejects_conflicting_configured_reference() -> None:
    with pytest.raises(ValueError, match="conflicts"):
        FishAudioTTS(json.dumps({"fish_audio_ak": "fixture-token", "fish_audio_refid": OTHER_VOICE_ID}), VOICE_ID)


@pytest.mark.parametrize("reference", [True, 12, [], {}, " padded-reference "])
def test_reference_rejects_invalid_shape(reference: Any) -> None:
    with pytest.raises(ValueError, match="reference ID"):
        FishAudioTTS(json.dumps({"fish_audio_ak": "fixture-token", "fish_audio_refid": reference}), VOICE_ID)


def test_legacy_name_without_reference_keeps_optional_reference() -> None:
    model = FishAudioTTS(json.dumps({"fish_audio_ak": "fixture-token"}), "legacy-model")
    assert model.ref_id is None
