"""Generation configuration belongs to the caller, including nested SDK options."""

import ast
import asyncio
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.llm import chat, cv
from core.llm.chat_model.base import Base as SyncBase
from core.llm.cv_model.models.anthropic_cv import AnthropicCV as SyncAnthropicCV


def test_generation_config_defaults_are_immutable() -> None:
    root = Path(__file__).resolve().parents[2] / "core/llm"
    paths = [root / "chat.py", root / "cv.py", *(root / "chat_model").rglob("*.py"), *(root / "cv_model").rglob("*.py")]
    violations: list[str] = []
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = [*node.args.posonlyargs, *node.args.args]
            defaults = list(zip(positional[-len(node.args.defaults) :], node.args.defaults)) if node.args.defaults else []
            defaults.extend(zip(node.args.kwonlyargs, node.args.kw_defaults))
            for arg, default in defaults:
                if arg.arg == "gen_conf" and isinstance(default, (ast.Dict, ast.List, ast.Set)):
                    violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not violations, violations


def _config() -> dict[str, Any]:
    return {"max_tokens": 17, "stop": ["end"], "response_format": {"type": "json_schema", "json_schema": {"schema": {"required": ["answer"]}}}}


def _mutate_sdk_options(options: dict[str, Any], seen: list[dict[str, Any]]) -> None:
    snapshot = {key: deepcopy(options[key]) for key in ("stop", "response_format") if key in options}
    seen.append(snapshot)
    options.get("stop", []).append("provider-stop")
    options.get("response_format", {}).get("json_schema", {}).get("schema", {}).get("required", []).append("provider-field")


def _response() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="answer", tool_calls=None), finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=3, prompt_tokens=1, completion_tokens=2),
    )


def _chunk() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content="answer", tool_calls=None, reasoning_content=None), finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=3, prompt_tokens=1, completion_tokens=2),
    )


class _Stream:
    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def __aiter__(self) -> AsyncIterator[SimpleNamespace]:
        yield _chunk()

    def __iter__(self) -> Iterator[SimpleNamespace]:
        yield _chunk()

    async def aclose(self) -> None:
        pass


@pytest.mark.parametrize("method", ["chat", "chat_streamly"])
def test_omitted_configuration_starts_fresh_across_instances(method: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def prepare(_self: Any, _system: str, _history: list, config: dict[str, Any]) -> str:
        seen.append(deepcopy(config))
        config.setdefault("stop", []).append("local-runtime-option")
        return "prompt"

    def respond(*args: Any) -> Iterator[str | int]:
        yield "answer"
        yield 3

    monkeypatch.setattr(chat.LocalLLM, "_prepare_prompt", prepare)
    monkeypatch.setattr(chat.LocalLLM, "_stream_response", respond)
    models = [chat.LocalLLM.__new__(chat.LocalLLM) for _ in range(2)]
    for model in [models[0], models[0], models[1]]:
        result = getattr(model, method)("", [])
        assert list(result) == ["answer", 3]
    assert seen == [{}, {}, {}]


@pytest.mark.parametrize("backend", ["base", "litellm"])
@pytest.mark.parametrize("method", ["async_chat", "async_chat_streamly", "async_chat_with_tools", "async_chat_streamly_with_tools"])
async def test_async_calls_keep_shared_nested_configuration(backend: str, method: str, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    before = deepcopy(config)
    seen: list[dict[str, Any]] = []

    async def complete(**options: Any) -> Any:
        _mutate_sdk_options(options, seen)
        await asyncio.sleep(0)
        return _Stream() if options.get("stream") else _response()

    models = [
        chat.Base("fixture", "fixture", "https://fixture.invalid", max_retries=0) if backend == "base" else chat.LiteLLMBase("fixture", "fixture", provider="OpenAI", max_retries=0) for _ in range(2)
    ]
    if backend == "litellm":
        monkeypatch.setattr(chat.litellm, "acompletion", complete)
    else:
        for model in models:
            model.async_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))

    async def invoke(model: Any) -> None:
        result = getattr(model, method)("", [{"role": "user", "content": "hello"}], config)
        if "streamly" in method:
            assert [part async for part in result] == ["answer", 3]
        else:
            assert await result == ("answer", 3)

    await asyncio.gather(*(invoke(model) for model in models))
    assert config == before
    assert len(seen) == 2
    assert all(snapshot == {key: before[key] for key in ("stop", "response_format")} for snapshot in seen)


@pytest.mark.parametrize("method", ["chat", "chat_streamly", "chat_with_tools", "chat_streamly_with_tools"])
def test_sync_calls_keep_shared_configuration(method: str) -> None:
    config = _config()
    if method == "chat_streamly":
        # The legacy sync streamer has a separate stop-kwarg collision; exercise
        # nested response_format here without expanding this isolation fix.
        config.pop("stop")
    before = deepcopy(config)
    seen: list[dict[str, Any]] = []

    def complete(**options: Any) -> Any:
        _mutate_sdk_options(options, seen)
        return _Stream() if options.get("stream") else _response()

    models = [SyncBase.__new__(SyncBase) for _ in range(2)]
    for model in models:
        model.model_name, model.max_retries, model.max_rounds, model.tools = "fixture", 0, 1, []
        model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))

    def invoke(model: Any) -> None:
        result = getattr(model, method)("", [{"role": "user", "content": "hello"}], config)
        if "streamly" in method:
            assert list(result) == ["answer", 3]
        else:
            assert result == ("answer", 3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(invoke, models))
    assert config == before
    assert len(seen) == 2
    assert all(snapshot == {key: before[key] for key in ("stop", "response_format") if key in before} for snapshot in seen)


@pytest.mark.parametrize("model_class", [cv.Zhipu4V, cv.NvidiaCV])
def test_vision_request_copies_nested_options_and_default(model_class: type, monkeypatch: pytest.MonkeyPatch) -> None:
    config = {"extra_body": {"sampling": ["caller"]}}
    before = deepcopy(config)
    seen: list[dict[str, Any]] = []

    def post(*args: Any, **kwargs: Any) -> SimpleNamespace:
        payload = kwargs["json"]
        seen.append(deepcopy(payload))
        payload.setdefault("extra_body", {"sampling": []})["sampling"].append("provider")
        return SimpleNamespace(json=lambda: {"answer": "fixture"})

    monkeypatch.setattr(cv.requests, "post", post)
    model = model_class.__new__(model_class)
    model.base_url, model.model_name, model.api_key, model.key = "https://fixture.invalid", "fixture", "fixture", "fixture"
    args = ([], False) if model_class is cv.Zhipu4V else ([],)
    assert model._request(*args, config) == {"answer": "fixture"}
    assert config == before
    model._request(*args)
    model._request(*args)
    assert "extra_body" not in seen[-1]


@pytest.mark.parametrize("stream", [False, True])
async def test_anthropic_vision_cleaning_preserves_callers_config(stream: bool) -> None:
    config = {**_config(), "presence_penalty": 0.4}
    before = deepcopy(config)
    seen: list[dict[str, Any]] = []

    async def complete(**options: Any) -> Any:
        _mutate_sdk_options(options, seen)
        assert options["max_tokens"] == 17 and "presence_penalty" not in options
        if stream:

            class Events:
                async def __aiter__(self) -> AsyncIterator[SimpleNamespace]:
                    yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="answer"))

            return Events()
        return SimpleNamespace(to_dict=lambda: {"content": [{"type": "text", "text": "answer"}], "usage": {"input_tokens": 1, "output_tokens": 2}, "stop_reason": "end_turn"})

    model = cv.AnthropicCV.__new__(cv.AnthropicCV)
    model.model_name, model.max_tokens = "fixture", 1024
    model.async_client = SimpleNamespace(messages=SimpleNamespace(create=complete))
    if stream:
        assert "answer" in [part async for part in model.async_chat_streamly("", [], config)]
    else:
        assert await model.async_chat("", [], config) == ("answer", 3)
    assert config == before


def test_sync_vision_cleaning_preserves_callers_config() -> None:
    config = {**_config(), "presence_penalty": 0.4, "frequency_penalty": 0.2}
    before = deepcopy(config)

    def complete(**options: Any) -> Any:
        _mutate_sdk_options(options, [])
        assert "presence_penalty" not in options and "frequency_penalty" not in options
        return SimpleNamespace(to_dict=lambda: {"content": [{"text": "answer"}], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 2}})

    model = SyncAnthropicCV.__new__(SyncAnthropicCV)
    model.model_name, model.max_tokens = "fixture", 1024
    model.client = SimpleNamespace(messages=SimpleNamespace(create=complete))
    assert model.chat("", [], config) == ("answer", 3)
    assert config == before
