"""Explicit tool capability must survive credentials, model lookup and execution."""

import base64
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

import core.llm as provider_models
from agent.component.agent_with_tools import Agent
from api.apps import llm_app
from api.db.joint_services import tenant_model_service
from api.db.services.llm_service import LLMBundle, LLMService
from api.db.services.tenant_llm_service import TenantLangfuseService, TenantLLMService, TenantService
from core.llm.cv import Base as VisionBase


@pytest.fixture(autouse=True)
def unmanaged_legacy_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe tests isolate the legacy management boundary; SQL guards are tested separately."""
    monkeypatch.setattr(llm_app, "require_legacy_write", lambda *_args: None)


@pytest.mark.parametrize("is_tools", [True, False, None])
@pytest.mark.parametrize(
    "provider_fields", [{}, {"group_id": "fixture-group", "opaque": {"retain": True}}, {"provider_order": ["first", "second"]}, {"auth_mode": "assume_role", "bedrock_region": "fixture-region"}]
)
def test_key_codec_preserves_provider_configuration(is_tools: bool | None, provider_fields: dict[str, Any]) -> None:
    original = {"api_key": "fixture-key", **provider_fields}
    raw = json.dumps(original)
    encoded = TenantLLMService._encode_api_key_config(raw, is_tools)
    key, resolved, payload = TenantLLMService._decode_api_key_config(encoded)
    assert key == "fixture-key" and resolved is is_tools
    assert json.loads(payload) == original if provider_fields else payload is None
    assert json.loads(raw) == original
    assert TenantLLMService._encode_api_key_config("plain-key", None) == "plain-key"


@pytest.mark.parametrize("bad", ["false", "true", 0, 1, None])
def test_capability_is_a_strict_boolean(bad: Any) -> None:
    with pytest.raises(ValidationError):
        llm_app.AddLLMRequest(llm_factory="Ollama", llm_name="fixture", mdl_type="chat", is_tools=bad)
    with pytest.raises(ValueError, match="Stored is_tools must be a boolean"):
        TenantLLMService._decode_api_key_config(json.dumps({"api_key": "fixture-key", "is_tools": bad}))


@pytest.mark.parametrize("path", ["id", "name", "legacy"])
@pytest.mark.parametrize("override", [True, False, None])
def test_all_model_lookup_paths_honor_explicit_capability(monkeypatch: pytest.MonkeyPatch, db: Session, path: str, override: bool | None) -> None:
    raw = TenantLLMService._encode_api_key_config('{"api_key":"fixture-key","group_id":"group"}', override)
    record = SimpleNamespace(to_dict=lambda: {"id": 17, "llm_name": "fixture", "llm_factory": "Ollama", "mdl_type": "chat", "api_key": raw, "api_base": "http://fixture.invalid/v1"})
    monkeypatch.setattr(TenantLLMService, "get_or_none", classmethod(lambda cls, db, **filters: record))
    monkeypatch.setattr(TenantLLMService, "get_api_key", classmethod(lambda cls, *args, **kwargs: record))
    monkeypatch.setattr(TenantService, "get_by_id", classmethod(lambda cls, *args: SimpleNamespace(llm_id="fixture")))
    monkeypatch.setattr(LLMService, "query", classmethod(lambda cls, db, **filters: [SimpleNamespace(is_tools=True)]))
    if path == "id":
        config = tenant_model_service.get_model_config_by_id(db, 17, tenant_id="tenant")
    elif path == "name":
        config = tenant_model_service.get_model_config_by_type_and_name(db, "tenant", "chat", "fixture")
    else:
        config = TenantLLMService.get_model_config(db, "tenant", "chat", "fixture")
    assert config["is_tools"] is (True if override is None else override)
    assert config["api_key"] == "fixture-key"
    assert json.loads(config["api_key_payload"])["group_id"] == "group"
    assert "is_tools" not in json.loads(config["api_key_payload"])


@pytest.mark.parametrize("new_key", [None, "replacement-key"])
@pytest.mark.parametrize("override", [True, False, None])
async def test_capability_edit_preserves_structured_credentials(monkeypatch: pytest.MonkeyPatch, db: Session, new_key: str | None, override: bool | None) -> None:
    before = {"api_key": "fixture-key", "group_id": "fixture-group", "opaque": {"retain": True}, "is_tools": True}
    saved: list[dict[str, Any]] = []
    keys: list[str] = []

    class Probe:
        def __init__(self, key: str, **kwargs: Any) -> None:
            keys.append(key)

        async def async_chat_streamly(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
            yield "ok"

    monkeypatch.setattr(llm_app, "get_allowed_llm_factories", lambda db: [SimpleNamespace(name="MiniMax")])
    monkeypatch.setattr(provider_models, "ChatModel", {"MiniMax": Probe})
    monkeypatch.setattr(TenantLLMService, "query", classmethod(lambda cls, db, **filters: [SimpleNamespace(api_key=json.dumps(before))]))
    monkeypatch.setattr(TenantLLMService, "filter_update", classmethod(lambda cls, db, filters, payload: saved.append(payload) or True))
    payload: dict[str, Any] = {"llm_factory": "MiniMax", "llm_name": "fixture", "mdl_type": "chat"}
    if new_key is not None:
        payload["api_key"] = new_key
    if override is not None:
        payload["is_tools"] = override
    response = await llm_app.add_llm(llm_app.AddLLMRequest(**payload), db, SimpleNamespace(id="tenant"))
    assert json.loads(response.body)["retcode"] == 0
    after = json.loads(saved[0]["api_key"])
    assert after == {**before, "api_key": new_key or "fixture-key", "is_tools": True if override is None else override}
    assert json.loads(keys[0]) == {key: value for key, value in after.items() if key != "is_tools"}


@pytest.mark.parametrize("factory", ["MinerU", "PaddleOCR", "OpenDataLoader"])
@pytest.mark.parametrize("edit", ["create", "replace", "capability", "flat_saved"])
async def test_ocr_object_key_survives_capability_merge(monkeypatch: pytest.MonkeyPatch, db: Session, factory: str, edit: str) -> None:
    before = {"endpoint": "http://owned.invalid/ocr", "token": "fixture-original"}
    replacement = {**before, "token": "fixture-replacement"}
    saved: list[dict[str, Any]] = []
    probed: list[dict[str, Any]] = []

    class Probe:
        def __init__(self, key: str, **kwargs: Any) -> None:
            probed.append(json.loads(key))

        def check_available(self) -> tuple[bool, str]:
            return True, ""

    record = SimpleNamespace(api_key=json.dumps(before if edit == "flat_saved" else {"api_key": before, "provider_order": "", "is_tools": True}), api_base="", max_tokens=0)
    monkeypatch.setattr(llm_app, "get_allowed_llm_factories", lambda db: [SimpleNamespace(name=factory)])
    monkeypatch.setattr(provider_models, "OcrModel", {factory: Probe})
    monkeypatch.setattr(TenantLLMService, "query", classmethod(lambda cls, db, **filters: [] if edit == "create" else [record]))
    monkeypatch.setattr(TenantLLMService, "filter_update", classmethod(lambda cls, db, filters, payload: saved.append(payload) or True))
    payload: dict[str, Any] = {"llm_factory": factory, "llm_name": "fixture", "mdl_type": "ocr", "is_tools": False}
    if edit in {"create", "replace"}:
        payload["api_key"] = replacement
    response = await llm_app.add_llm(llm_app.AddLLMRequest(**payload), db, SimpleNamespace(id="tenant"))
    assert json.loads(response.body)["retcode"] == 0
    expected = replacement if edit in {"create", "replace"} else before
    assert json.loads(saved[0]["api_key"]) == {"api_key": expected, "provider_order": "", "is_tools": False}
    assert probed == [{"api_key": expected, "provider_order": ""}]


@pytest.mark.parametrize("enabled", [True, False])
async def test_bundle_binds_and_dispatches_native_tools_only_when_enabled(monkeypatch: pytest.MonkeyPatch, db: Session, enabled: bool) -> None:
    calls: list[str] = []

    class Model:
        is_tools = False

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def bind_tools(self, *args: Any) -> None:
            self.is_tools = True
            calls.append("bind")

        async def async_chat(self, *args: Any, **kwargs: Any) -> tuple[str, int]:
            calls.append("plain")
            return "answer", 0

        async def async_chat_with_tools(self, *args: Any, **kwargs: Any) -> tuple[str, int]:
            calls.append("tools")
            return "answer", 0

    monkeypatch.setattr(provider_models, "ChatModel", {"Ollama": Model})
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", classmethod(lambda cls, *args, **kwargs: None))
    config = {"llm_name": "fixture", "llm_factory": "Ollama", "mdl_type": "chat", "api_key": TenantLLMService._encode_api_key_config("fixture-key", enabled), "api_base": "http://fixture.invalid"}
    bundle = LLMBundle(db, "tenant", config)
    bundle.bind_tools(object(), [{"type": "function", "function": {"name": "first"}}])
    assert await bundle.async_chat("", [{"role": "user", "content": "hello"}], {}) == "answer"
    assert calls == (["bind", "tools"] if enabled else ["plain"])


@pytest.mark.parametrize("fallback", ["safe configured fallback", ""])
async def test_agent_tool_error_emits_one_consistent_output(fallback: str) -> None:
    agent = object.__new__(Agent)
    agent._param = SimpleNamespace(cite=False)
    outputs: dict[str, Any] = {}
    agent.set_output = lambda key, value: outputs.__setitem__(key, value)
    agent._fit_messages = lambda prompt, messages: messages
    agent.check_if_canceled = lambda context: False
    getter = Mock(return_value=fallback)
    agent.get_exception_default_value = getter

    async def generate(*args: Any) -> AsyncIterator[str]:
        yield "**ERROR**: fixture failure"

    agent._generate_streamly = generate
    result = [part async for part in agent.stream_output_with_tools_async("", [{"role": "user", "content": "hello"}])]
    expected = fallback or "**ERROR**: fixture failure"
    assert result == [expected] and outputs["content"] == expected
    assert outputs.get("_ERROR") == (None if fallback else expected)
    getter.assert_called_once_with()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("video", [False, True])
async def test_vision_tool_bridge_preserves_images_and_video_frames(monkeypatch: pytest.MonkeyPatch, stream: bool, video: bool) -> None:
    captured: list[list[dict[str, Any]]] = []

    class ToolsBackend:
        is_tools = False

        def bind_tools(self, *args: Any) -> None:
            self.is_tools = True

        async def async_chat_with_tools(self, system: str, history: list[dict[str, Any]], conf: dict[str, Any]) -> tuple[str, int]:
            captured.append(history)
            return "answer", 0

        async def async_chat_streamly_with_tools(self, system: str, history: list[dict[str, Any]], conf: dict[str, Any]) -> AsyncIterator[str]:
            captured.append(history)
            yield "answer"

    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=")
    factory = Mock(return_value=ToolsBackend())
    model = VisionBase(_tool_chat_model_factory=factory)
    factory.assert_not_called()
    frames = Mock(return_value=[png])
    monkeypatch.setattr(model, "_video_frames_to_image_bytes", frames)
    model.bind_tools(object(), [{"type": "function", "function": {"name": "first"}}])
    factory.assert_called_once_with()
    history = [{"role": "user", "content": "hello"}]
    options = {"images": [png], "video_bytes": b"fixture-video" if video else None, "filename": "clip.mp4"}
    if stream:
        assert [part async for part in model.async_chat_streamly_with_tools("", history, {}, **options)] == ["answer"]
    else:
        assert await model.async_chat_with_tools("", history, {}, **options) == ("answer", 0)
    assert model.is_tools is True and history == [{"role": "user", "content": "hello"}]
    content = captured[0][0]["content"]
    assert content[0] == {"type": "text", "text": "hello"}
    assert len(content) == (3 if video else 2)
    assert all(part["image_url"]["url"].startswith("data:image/png;base64,") for part in content[1:])
    if video:
        frames.assert_called_once_with(b"fixture-video", "clip.mp4")
    else:
        frames.assert_not_called()


@pytest.mark.parametrize("factory", ["VLLM", "MiniMax", "VolcEngine"])
@pytest.mark.parametrize("edit", ["omitted", "null", "empty", "explicit", "ark_only", "endpoint_only"])
async def test_partial_credentials_are_merged_before_provider_probe(monkeypatch: pytest.MonkeyPatch, db: Session, factory: str, edit: str) -> None:
    before: dict[str, Any] = {"is_tools": False}
    if factory != "VLLM":
        before["opaque"] = {"retain": True}
    if factory == "VolcEngine":
        before.update(ark_api_key="saved-ark", endpoint_id="saved-model")
    else:
        before["api_key"] = "saved-key"
        if factory == "MiniMax":
            before["group_id"] = "saved-group"
    saved: list[dict[str, Any]] = []
    probed: list[str] = []

    class Probe:
        def __init__(self, key: str, **kwargs: Any) -> None:
            probed.append(key)

        async def async_chat_streamly(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
            yield "ok"

    def query(cls: type[TenantLLMService], session: Session, **filters: Any) -> list[SimpleNamespace]:
        assert filters == {"tenant_id": "tenant", "llm_factory": factory, "llm_name": "fixture" + ("___VLLM" if factory == "VLLM" else "")}
        return [SimpleNamespace(api_key=json.dumps(before), api_base="http://fixture.invalid/v1", max_tokens=4096)]

    monkeypatch.setattr(llm_app, "get_allowed_llm_factories", lambda db: [SimpleNamespace(name=factory)])
    monkeypatch.setattr(provider_models, "ChatModel", {factory: Probe})
    monkeypatch.setattr(TenantLLMService, "query", classmethod(query))
    monkeypatch.setattr(TenantLLMService, "filter_update", classmethod(lambda cls, db, filters, payload: saved.append(payload) or True))
    fields: dict[str, Any] = {"llm_factory": factory, "llm_name": "fixture", "mdl_type": "chat"}
    if edit == "null":
        fields["api_key"] = None
    elif edit == "empty":
        fields["api_key"] = ""
    elif edit == "explicit":
        fields["api_key"] = {"ark_api_key": "new-ark"} if factory == "VolcEngine" else "new-key"
    elif edit == "ark_only":
        fields["ark_api_key"] = "new-ark"
    elif edit == "endpoint_only":
        fields["endpoint_id"] = "new-model"
    response = await llm_app.add_llm(llm_app.AddLLMRequest(**fields), db, SimpleNamespace(id="tenant"))
    assert json.loads(response.body)["retcode"] == 0
    expected = dict(before)
    if factory == "VolcEngine":
        if edit in {"explicit", "ark_only"}:
            expected["ark_api_key"] = "new-ark"
        elif edit == "endpoint_only":
            expected["endpoint_id"] = "new-model"
    elif edit in {"explicit", "empty"}:
        expected["api_key"] = "new-key" if edit == "explicit" else "x"
    assert json.loads(saved[0]["api_key"]) == expected
    if factory == "VLLM":
        assert probed[0] == expected["api_key"]
    else:
        assert json.loads(probed[0]) == {k: v for k, v in expected.items() if k != "is_tools"}
    assert saved[0]["api_base"] == "http://fixture.invalid/v1" and saved[0]["max_tokens"] == 4096
