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
from api.db.db_models import TenantModel, TenantModelInstance, TenantModelProvider
from api.db.joint_services import tenant_model_service
from api.db.model_contract import InstanceModelInput
from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import TenantLangfuseService, TenantLLMService
from core.llm.cv import Base as VisionBase


@pytest.mark.parametrize("is_tools", [True, False, None])
@pytest.mark.parametrize(
    "provider_fields", [{}, {"group_id": "fixture-group", "opaque": {"retain": True}}, {"provider_order": ["first", "second"]}, {"auth_mode": "assume_role", "bedrock_region": "fixture-region"}]
)
def test_key_codec_preserves_provider_configuration(is_tools: bool | None, provider_fields: dict[str, Any]) -> None:
    original = {"api_key": "fixture-key", **provider_fields}
    raw = json.dumps(original)
    encoded = json.dumps({**original, **({"is_tools": is_tools} if is_tools is not None else {})})
    key, resolved, payload = TenantLLMService._decode_api_key_config(encoded)
    assert key == "fixture-key" and resolved is is_tools
    assert json.loads(payload) == original if provider_fields else payload is None
    assert json.loads(raw) == original
    assert TenantLLMService._decode_api_key_config("plain-key") == ("plain-key", None, None)


@pytest.mark.parametrize("bad", ["false", "true", 0, 1, None])
def test_capability_is_a_strict_boolean(bad: Any) -> None:
    with pytest.raises(ValidationError):
        InstanceModelInput(model_name="fixture", model_type=["chat"], max_tokens=0, extra={"is_tools": bad})
    with pytest.raises(ValueError, match="Stored is_tools must be a boolean"):
        TenantLLMService._decode_api_key_config(json.dumps({"api_key": "fixture-key", "is_tools": bad}))


@pytest.mark.parametrize("path", ["id", "reference", "default"])
@pytest.mark.parametrize("override", [True, False])
def test_all_model_lookup_paths_honor_explicit_capability(monkeypatch: pytest.MonkeyPatch, db: Session, path: str, override: bool) -> None:
    identifier = "a" * 32
    model = TenantModel(id=identifier, model_name="fixture", model_type=1, status="active", extra={"is_tools": override})
    instance = TenantModelInstance(
        id="b" * 32, api_key=json.dumps({"api_key": "fixture-key", "group_id": "group", "is_tools": not override}), base_url="http://fixture.invalid/v1", region="", status="active"
    )
    provider = TenantModelProvider(tenant_id="tenant", provider_name="Ollama", status="active")
    monkeypatch.setattr(tenant_model_service, "model_for_id", lambda *args, **kwargs: (model, instance, provider))
    monkeypatch.setattr(tenant_model_service.TenantService, "get_or_none", lambda session, **filters: SimpleNamespace(tenant_llm_id=identifier))
    if path == "id":
        config = tenant_model_service.get_model_config_by_id(db, identifier, tenant_id="tenant")
    elif path == "reference":
        config = tenant_model_service.get_model_config_by_type_and_name(db, "tenant", "chat", identifier)
    else:
        config = TenantLLMService.get_model_config(db, "tenant", "chat")
    assert config["is_tools"] is override
    assert config["api_key"] == "fixture-key"
    assert json.loads(config["api_key_payload"]) == {"api_key": "fixture-key", "group_id": "group"}


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
    config = {"llm_name": "fixture", "llm_factory": "Ollama", "mdl_type": "chat", "api_key": "fixture-key", "is_tools": enabled, "api_base": "http://fixture.invalid"}
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
