"""Frozen provider batch: real registered CV/chat request policies and catalog identity."""

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from api.db.model_contract import ConnectionCandidate, ModelRegistration
from api.db.provider_catalog import normalize_model_transport, provider_order, provider_transport
from api.db.services import model_provider_service, provider_verification
from common import settings
from core.llm import ChatModel, CvModel, chat, cv


@pytest.mark.parametrize("name,disabled", [("Qwen3.5-Plus", True), ("qwen3.6-plus", True), ("qwen2.5-vl", False)])
async def test_active_qwen_cv_final_image_stream_and_video_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, disabled: bool) -> None:
    assert CvModel["Tongyi-Qianwen"] is cv.QWenCV
    model = cv.QWenCV("fixture", name)
    seen: list[dict[str, Any]] = []
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))], usage=SimpleNamespace(total_tokens=0))

    def sync_create(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return response

    class Stream:
        async def __aenter__(self) -> "Stream":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        def __aiter__(self) -> Any:
            async def chunks() -> Any:
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="OK"), finish_reason="stop")], usage=None)

            return chunks()

    async def async_create(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return Stream() if kwargs.get("stream") else response

    monkeypatch.setattr(model.client.chat.completions, "create", sync_create)
    monkeypatch.setattr(model.async_client.chat.completions, "create", async_create)
    config = {"temperature": 0.3, "top_p": 0.6}
    original = deepcopy(config)
    try:
        model.describe_with_prompt(b"image", "Read")
        assert (await model.async_chat("", [{"role": "user", "content": "Read"}], config, images=[b"image"]))[0] == "OK"
        chunks = [item async for item in model.async_chat_streamly("", [{"role": "user", "content": "Read"}], config, images=[b"image"])]
        assert chunks[0] == "OK"
        assert config == original
        assert all(item["extra_body"] == ({"enable_thinking": False} if disabled else None) for item in seen)
        assert all("temperature" not in item and "top_p" not in item for item in seen)
        import dashscope

        def native(**kwargs: Any) -> dict[str, Any]:
            assert kwargs.get("enable_thinking") is (False if disabled else None)
            return {"output": {"choices": [{"message": SimpleNamespace(content=[{"text": "OK"}])}]}}

        from tests.support.resource_journal import ResourceJournal

        native_sample = tmp_path / "native-video.mp4"
        journal = ResourceJournal("qwen-native-video")
        journal.event("reserve-file", path=str(native_sample))

        @contextmanager
        def owned_video(**kwargs: Any) -> Any:
            with native_sample.open("xb") as output:
                yield output

        monkeypatch.setattr(cv.tempfile, "NamedTemporaryFile", owned_video)
        monkeypatch.setattr(dashscope.MultiModalConversation, "call", native)
        assert model._process_video(b"video", "sample.mp4", "Read")[0] == "OK"
        assert not native_sample.exists()
        journal.event("file-independent-readback", path=str(native_sample), absent=True)
    finally:
        model.client.close()
        await model.async_client.close()


@pytest.mark.parametrize(
    "provider,name,expected",
    [
        ("Anthropic", "claude-opus-4-7", set()),
        ("Anthropic", "claude-opus-4-8", set()),
        ("Bedrock", "eu.anthropic.claude-sonnet-4-6-v1:0", {"temperature"}),
        ("Bedrock", "us.anthropic.claude-opus-4-6-v1:0", {"temperature"}),
        ("Anthropic", "claude-sonnet-4-20250514", {"temperature", "top_p"}),
        ("Anthropic", "claude-3-5-sonnet-20241022", {"temperature", "top_p"}),
    ],
)
@pytest.mark.parametrize("mode", ["chat", "stream", "tools", "tools_stream"])
async def test_claude_final_completion_boundary(monkeypatch: pytest.MonkeyPatch, provider: str, name: str, expected: set[str], mode: str) -> None:
    assert ChatModel[provider] is chat.LiteLLMBase
    key = json.dumps({"auth_mode": "access_key_secret", "bedrock_region": "us-east-1", "bedrock_ak": "fixture", "bedrock_sk": "fixture"}) if provider == "Bedrock" else "fixture"
    model = chat.LiteLLMBase(key, name, provider=provider, max_retries=0)
    model.tools = []
    seen: list[dict[str, Any]] = []

    @asynccontextmanager
    async def completion(options: dict[str, Any]) -> Any:
        seen.append(options)
        message = SimpleNamespace(content="OK", tool_calls=None, reasoning_content=None)
        if "stream" in options:

            async def chunks() -> Any:
                yield SimpleNamespace(choices=[SimpleNamespace(delta=message, finish_reason="stop")], usage=None)

            yield chunks()
        else:
            yield SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None)

    monkeypatch.setattr(model, "_completion", completion)
    config = {"temperature": 0.0, "top_p": 0.8}
    history = [{"role": "user", "content": "Hello"}]
    if mode == "chat":
        await model.async_chat("", history, config, top_p=0.9)
    elif mode == "stream":
        _ = [item async for item in model.async_chat_streamly("", history, config)]
    elif mode == "tools":
        await model.async_chat_with_tools("", history, config)
    else:
        _ = [item async for item in model.async_chat_streamly_with_tools("", history, config)]
    assert seen
    assert {key for key in seen[0] if key in {"temperature", "top_p"}} == expected
    assert config == {"temperature": 0.0, "top_p": 0.8}


def test_catalog_entries_region_identity_and_total_order(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[2]
    factories = json.loads((root / "configs/llm_factories.json").read_text())["factory_llm_infos"]
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", factories)
    assert {"MiniMax-M3"} <= {item.model_name for item in model_provider_service.catalog("MiniMax")}
    assert {"claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6"} <= {item.model_name for item in model_provider_service.catalog("Anthropic")}
    assert {"voyage-4", "voyage-4-large", "voyage-4-lite", "rerank-2.5", "rerank-2.5-lite"} <= {item.model_name for item in model_provider_service.catalog("Voyage AI")}
    names = ["unknown-local", "OpenAI", "Anthropic", "BAAI"]
    assert sorted(names, key=provider_order) == sorted(names[1:] + names[:1], key=provider_order)
    assert provider_transport("SILICONFLOW", "raw", "", "intl") == ("raw", "https://api.siliconflow.com/v1", "intl")
    legacy = normalize_model_transport({"llm_factory": "SILICONFLOW", "api_key": json.dumps({"api_key": "raw", "source_fid": "siliconflow_intl"})})
    assert legacy["region"] == "intl" and legacy["api_key"] == "raw"
    assert "siliconflow_intl" not in {item["name"] for item in model_provider_service.public_factories()}
    for region, url in [("bad", ""), ("cn", "https://api.siliconflow.com/v1"), ("intl", "https://api.siliconflow.cn/v1")]:
        with pytest.raises(ValueError):
            provider_transport("SILICONFLOW", "raw", url, region)
    with pytest.raises(ValueError):
        model_provider_service.candidate_models("OpenAI", ConnectionCandidate(api_key="fixture", model_ids=[]))


@pytest.mark.parametrize("failure", ["constructor", "error", "empty", "timeout", "cancel"])
async def test_probe_failure_cancellation_and_owned_close(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    closed: list[bool] = []

    class Adapter:
        def __init__(self, **kwargs: Any) -> None:
            if failure == "constructor":
                raise ValueError("SECRET")

        async def async_chat(self, *args: Any) -> tuple[str, int]:
            if failure == "timeout":
                await asyncio.sleep(1)
            if failure == "cancel":
                raise asyncio.CancelledError
            return ("**ERROR** SECRET" if failure == "error" else ""), 0

        async def aclose(self) -> None:
            closed.append(True)

    import core.llm

    monkeypatch.setattr(core.llm, "ChatModel", {"OpenAI": Adapter})
    monkeypatch.setenv("LLM_PROBE_TIMEOUT_SECONDS", "0.01" if failure == "timeout" else "1")
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError) as error:
        await provider_verification.verify_model("OpenAI", "SECRET", "", ModelRegistration(model_name="test", model_types=["chat"], max_tokens=0))
    assert "SECRET" not in str(error.value)
    assert closed == [True]


@pytest.mark.parametrize(
    "capability,values,success",
    [("embedding", [[0.0, 1.0]], True), ("embedding", [[]], False), ("embedding", [[float("nan")]], False), ("rerank", [0.0], True), ("rerank", [False], False), ("rerank", [], False)],
)
async def test_probe_vectors_require_finite_shape_but_accept_zero_usage(monkeypatch: pytest.MonkeyPatch, capability: str, values: Any, success: bool) -> None:
    import core.llm

    closed: list[bool] = []

    class Adapter:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def encode(self, texts: list[str]) -> tuple[Any, int]:
            return values, 0

        def similarity(self, query: str, texts: list[str]) -> tuple[Any, int]:
            return values, 0

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(core.llm, "EmbeddingModel" if capability == "embedding" else "RerankModel", {"fixture": Adapter})
    declaration = ModelRegistration(model_name="zero", model_types=[capability], max_tokens=0)
    if success:
        assert (await provider_verification.verify_model("fixture", "key", "", declaration))["verified"] is True
    else:
        with pytest.raises(ValueError):
            await provider_verification.verify_model("fixture", "key", "", declaration)
    assert closed == [True]


async def test_probe_asr_owns_sample_and_preserves_caller_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import core.llm
    from tests.support.resource_journal import ResourceJournal

    sample = tmp_path / "multirag-provider-probe-owned.wav"
    ResourceJournal("asr-probe-sample").event("reserve-file", path=str(sample))
    monkeypatch.setattr(provider_verification.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(provider_verification, "uuid4", lambda: SimpleNamespace(hex="owned"))

    class Adapter:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def transcription(self, path: str) -> tuple[str, int]:
            assert Path(path).read_bytes().startswith(b"RIFF")
            return "", 0

    monkeypatch.setattr(core.llm, "Seq2txtModel", {"fixture": Adapter})
    assert (await provider_verification.verify_model("fixture", "", "", ModelRegistration(model_name="asr", model_types=["speech2text"], max_tokens=0)))["verified"] is True
    assert not sample.exists()


async def test_paddle_probe_performs_real_keyless_inference_and_closes_listener() -> None:
    import base64
    import socket
    import threading
    from contextlib import ExitStack
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from uuid import uuid4

    from tests.support.resource_journal import ResourceJournal

    journal = ResourceJournal("paddle-provider-probe-http")
    seen: list[dict[str, Any]] = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return None

        def do_POST(self) -> None:
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert base64.b64decode(data["file"]).startswith(b"%PDF")
            assert "Authorization" not in self.headers
            seen.append(data)
            encoded = json.dumps({"errorCode": 0, "result": {"ocrResults": [{"prunedResult": {"rec_texts": ["Connection test"], "rec_boxes": [[0, 0, 80, 20]]}}]}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    address = ("127.0.0.1", 30000 + int(uuid4().hex[:4], 16) % 20000)
    journal.event("reserve-listener", address=list(address))
    with ExitStack() as cleanup:
        server = ThreadingHTTPServer(address, Provider, bind_and_activate=False)
        cleanup.callback(server.server_close)
        server.server_bind()
        server.server_activate()
        thread = threading.Thread(target=server.serve_forever, name="owned-ocr-probe")
        thread.start()
        cleanup.callback(thread.join, 5)
        cleanup.callback(server.shutdown)
        key = json.dumps({"api_key": {"paddleocr_api_url": f"http://{address[0]}:{address[1]}/ocr", "paddleocr_algorithm": "PP-OCRv5"}})
        result = await provider_verification.verify_model("PaddleOCR", key, "", ModelRegistration(model_name="ocr", model_types=["ocr"], max_tokens=0))
        assert result["verified"] is True and len(seen) == 1
    with socket.socket() as reader:
        assert reader.connect_ex(address) != 0
    assert not thread.is_alive()
    journal.event("listener-independent-readback", address=list(address), absent=True, thread_absent=True)


async def test_probe_cleanup_closes_every_owned_client_after_one_close_fails() -> None:
    seen: list[str] = []

    class SyncClient:
        def close(self) -> None:
            seen.append("sync")
            raise ValueError("private diagnostic")

    class AsyncClient:
        async def close(self) -> None:
            seen.append("async")

    with pytest.raises(ExceptionGroup):
        await provider_verification._close(SimpleNamespace(client=SyncClient(), async_client=AsyncClient()))
    assert seen == ["sync", "async"]


@pytest.mark.parametrize("explicit", [None, False, True])
def test_historical_siliconflow_tools_do_not_override_linked_metadata(explicit: bool | None) -> None:
    config = {"llm_factory": "SILICONFLOW", "api_key": json.dumps({"api_key": "key", "source_fid": "siliconflow_intl", "is_tools": True})}
    if explicit is not None:
        config["is_tools"] = explicit
    result = normalize_model_transport(config)
    assert result["is_tools"] is (True if explicit is None else explicit)
    assert result["api_key"] == "key" and result["region"] == "intl"
