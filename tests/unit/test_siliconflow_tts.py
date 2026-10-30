"""SiliconFlow speech requests and streaming follow the published MP3 contract."""

import json
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import requests

from common import settings
from core.llm import TTSModel
from core.llm.tts import SILICONFLOWTTS

MODELS = ("FunAudioLLM/CosyVoice2-0.5B", "fnlp/MOSS-TTSD-v0.5")


class Response(requests.Response):
    def __init__(self, status: int = 200, broken: bool = False) -> None:
        super().__init__()
        self.status_code = status
        self._content = b"controlled provider rejection"
        self.closed = 0
        self.broken = broken

    def iter_content(self, chunk_size: int | None = 1, decode_unicode: bool = False) -> Iterator[bytes]:
        yield b"ID3-first"
        if self.broken:
            raise OSError("controlled truncated stream")
        yield b"-last"

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("provider_base", ["", "https://proxy.example.test/mount/v1"])
def test_siliconflow_speech_request_contract(monkeypatch: pytest.MonkeyPatch, model: str, provider_base: str) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    response = Response()

    def post(url: str, **kwargs: Any) -> Response:
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr("core.llm.tts.requests.post", post)
    adapter = TTSModel["SILICONFLOW"]("synthetic-key", model, base_url=provider_base)
    assert isinstance(adapter, SILICONFLOWTTS)
    with closing(adapter.tts("[S1]Hello[S2]Good morning")) as stream:
        assert b"".join(stream) == b"ID3-first-last"
    assert response.closed == 1
    assert len(calls) == 1
    url, request = calls[0]
    assert url == (provider_base or "https://api.siliconflow.cn/v1") + "/audio/speech"
    assert request["headers"]["Authorization"] == "Bearer synthetic-key"
    assert request["timeout"] == 60 and request["stream"] is True
    assert request["json"] == {
        "model": model,
        "input": "[S1]Hello[S2]Good morning",
        "voice": model + ":anna",
        "response_format": "mp3",
        "sample_rate": 32000,
        "stream": True,
        "speed": 1,
        "gain": 0,
    }


@pytest.mark.parametrize("name", MODELS)
def test_siliconflow_catalog_declares_speech_models(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from api.db.provider_catalog import public_factories
    from api.db.services.model_provider_service import catalog

    factories = json.loads((Path(__file__).parents[2] / "configs/llm_factories.json").read_text())["factory_llm_infos"]
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", factories)
    matches = [model for model in catalog("SILICONFLOW") if model.model_name == name]
    assert len(matches) == 1
    assert matches[0].model_types == ["tts"]
    assert matches[0].max_tokens == 26214400
    assert matches[0].is_tools is False
    factory = next(item for item in public_factories() if item["name"] == "SILICONFLOW")
    assert "TTS" in factory["tags"].split(",")


@pytest.mark.parametrize("mode", ["complete", "early-close", "broken"])
def test_siliconflow_closes_consumed_or_failed_audio(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    response = Response(broken=mode == "broken")
    monkeypatch.setattr("core.llm.tts.requests.post", lambda *args, **kwargs: response)
    with closing(SILICONFLOWTTS("synthetic-key").tts("Hello")) as stream:
        if mode == "broken":
            with pytest.raises(OSError, match="truncated stream"):
                list(stream)
        elif mode == "early-close":
            assert next(stream) == b"ID3-first"
        else:
            assert list(stream) == [b"ID3-first", b"-last"]
    assert response.closed == 1


@pytest.mark.parametrize("status", [400, 401, 429])
def test_siliconflow_closes_rejected_response(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    response = Response(status=status)
    monkeypatch.setattr("core.llm.tts.requests.post", lambda *args, **kwargs: response)
    with pytest.raises(Exception, match=str(status)):
        SILICONFLOWTTS("synthetic-key").tts("Hello")
    assert response.closed == 1
