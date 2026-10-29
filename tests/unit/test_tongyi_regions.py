"""Region selection reaches real registered adapters without SDK global mutation."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dashscope
import pytest

from api.db.provider_catalog import TONGYI_URLS, normalize_model_transport, provider_transport
from core.llm import ChatModel
from core.llm.cv import QWenCV
from core.llm.rerank import QWenRerank
from core.llm.sequence2txt import QWenSeq2txt
from core.llm.tts import QwenTTS


@pytest.mark.parametrize("region", ["", "default", "cn", "intl"])
def test_region_and_explicit_url_survive_runtime_resolution(region: str) -> None:
    expected = TONGYI_URLS["intl" if region == "intl" else "cn"]
    config = normalize_model_transport({"llm_factory": "Tongyi-Qianwen", "api_key": "fixture", "region": region})
    assert config["api_base"] == expected
    assert provider_transport("Tongyi-Qianwen", "fixture", "https://custom.invalid/mount/v1", region) == ("fixture", "https://custom.invalid/mount/v1", region)
    model = ChatModel["Tongyi-Qianwen"]("fixture", "qwen-plus", expected, provider="Tongyi-Qianwen")
    assert model.model_name == "openai/qwen-plus" and model.base_url == expected


@pytest.mark.parametrize("region", ["Intl", "us", "cn-beijing", " intl"])
def test_unknown_region_fails_closed_even_with_explicit_url(region: str) -> None:
    with pytest.raises(ValueError, match="Unsupported Tongyi"):
        provider_transport("Tongyi-Qianwen", "fixture", "https://custom.invalid/v1", region)


@pytest.mark.parametrize("region", ["cn", "intl"])
def test_native_rerank_and_audio_keep_endpoint_and_key_request_local(monkeypatch: pytest.MonkeyPatch, region: str) -> None:
    calls: list[dict[str, Any]] = []
    original = (dashscope.api_key, dashscope.base_http_api_url, dashscope.base_websocket_api_url)
    base = TONGYI_URLS[region]
    native = base.replace("/compatible-mode/v1", "/api/v1")

    def rerank(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, output={"results": [{"index": 0, "relevance_score": 0.5}]}, usage={"total_tokens": 0})

    def transcribe(**kwargs: Any) -> Any:
        calls.append(kwargs)
        result = {"output": {"choices": [{"message": SimpleNamespace(content=[{"text": "OK"}])}]}}
        return iter([result]) if kwargs.get("stream") else result

    from dashscope.audio.tts import SpeechSynthesisResult, SpeechSynthesizer

    def synthesize(**kwargs: Any) -> None:
        calls.append(kwargs)
        kwargs["callback"].on_event(SpeechSynthesisResult(b"audio", None, None, None, None))
        kwargs["callback"].on_complete()

    monkeypatch.setattr(dashscope.TextReRank, "call", rerank)
    monkeypatch.setattr(dashscope.MultiModalConversation, "call", transcribe)
    monkeypatch.setattr(SpeechSynthesizer, "call", synthesize)
    assert QWenRerank("fixture", "qwen3-rerank", base).similarity("q", ["doc"])[0].tolist() == [0.5]
    asr = QWenSeq2txt("fixture", base_url=base)
    assert asr.transcription("https://fixture.invalid/audio")[0] == "OK"
    assert list(asr.stream_transcription("https://fixture.invalid/audio"))[-1] == {"event": "final", "text": "OK"}
    audio = list(QwenTTS("fixture", "sambert-zhide-v1", base).tts("hello"))
    assert audio[0] == b"audio" and type(audio[-1]) is int
    assert all(call["api_key"] == "fixture" for call in calls)
    assert [call["base_address"] for call in calls] == [native] * 3 + [native.replace("https://", "wss://").replace("/api/v1", "/api-ws/v1/inference")]
    assert (dashscope.api_key, dashscope.base_http_api_url, dashscope.base_websocket_api_url) == original


async def test_video_provider_failure_does_not_switch_region_or_mutate_sdk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from contextlib import contextmanager

    from tests.support.resource_journal import ResourceJournal

    path = tmp_path / "video.mp4"
    ResourceJournal("tongyi-video-region").event("reserve-file", path=str(path))

    @contextmanager
    def temporary(**_kwargs: Any) -> Any:
        with path.open("xb") as stream:
            yield stream

    seen: list[str] = []
    original = dashscope.base_http_api_url

    def failed(**kwargs: Any) -> Any:
        seen.append(kwargs["base_address"])
        raise RuntimeError("controlled provider failure")

    monkeypatch.setattr("core.llm.cv.tempfile.NamedTemporaryFile", temporary)
    monkeypatch.setattr(dashscope.MultiModalConversation, "call", failed)
    model = QWenCV("fixture", "qwen-vl", base_url=TONGYI_URLS["intl"])
    try:
        with pytest.raises(RuntimeError, match="controlled provider failure"):
            model._process_video(b"video", "video.mp4", "summarize")
        assert seen == ["https://dashscope-intl.aliyuncs.com/api/v1"]
        assert dashscope.base_http_api_url == original and not path.exists()
    finally:
        model.client.close()
        await model.async_client.close()


async def test_ocr_probe_cancellation_drains_blocking_parse_before_closing(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import threading

    import core.llm
    from api.db.model_contract import ModelRegistration
    from api.db.services.provider_verification import verify_model

    entered, release = threading.Event(), threading.Event()
    events: list[str] = []

    class OCR:
        def __init__(self, **_kwargs: Any) -> None:
            return

        def parse_pdf(self, _name: str, **kwargs: Any) -> tuple[list[str], list[Any]]:
            assert kwargs["binary"].startswith(b"%PDF")
            entered.set()
            assert release.wait(5)
            events.append("parsed")
            return ["text"], []

        def close(self) -> None:
            events.append("closed")

    monkeypatch.setitem(core.llm.OcrModel, "fixture", OCR)
    task = asyncio.create_task(verify_model("fixture", "", "", ModelRegistration(model_name="ocr", model_types=["ocr"], max_tokens=0)))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not events
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["parsed", "closed"]
