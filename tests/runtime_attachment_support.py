"""Capture only the model boundary; keep the real chat adapter, Agent and Canvas."""

import sys
from collections.abc import AsyncGenerator
from contextlib import nullcontext
from copy import deepcopy
from typing import Any

import pytest


def capture_chat_model(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import agent.component.agent_with_tools as agent_module
    import agent.component.llm as llm_module

    state: dict[str, Any] = {"calls": [], "tools": False, "failure": None}

    class CapturedModel:
        max_length = 8192

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def aclose(self) -> None:
            pass

        async def async_chat_streamly_delta(self, system: str, history: list[dict], gen_conf: dict, **kwargs: Any) -> AsyncGenerator[str, None]:
            state["calls"].append(deepcopy({"system": system, "history": history, "images": kwargs.get("images", [])}))
            yield "captured"
            if state["failure"] == "exception":
                raise RuntimeError("model stream failed")
            if state["failure"] == "marker":
                yield "**ERROR**: provider failed"
                return
            yield " reply"

    monkeypatch.setattr(llm_module, "db_connection", lambda: nullcontext(None))
    monkeypatch.setattr(llm_module, "get_model_config_by_type_and_name", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(llm_module, "LLMBundle", CapturedModel)
    monkeypatch.setattr(llm_module, "resolve_model_type", lambda *_args: ["image2text"])
    monkeypatch.setattr(sys.modules["api.apps.llm"], "resolve_model_type", lambda *_args: ["image2text"])
    original_agent = agent_module.Agent

    def real_agent(canvas: Any, component_id: str, param: Any) -> Any:
        agent = original_agent(canvas, component_id, param)
        if state["tools"]:
            # Select the real tool-enabled generation branch; no external tool runs.
            agent.tools = {"capture_tool": object()}
        return agent

    monkeypatch.setattr(agent_module, "Agent", real_agent)
    return state


def sse_frames(text: str) -> list[dict[str, Any]]:
    import json

    return [json.loads(line.removeprefix("data:").strip()) for line in text.splitlines() if line.startswith("data:")]
