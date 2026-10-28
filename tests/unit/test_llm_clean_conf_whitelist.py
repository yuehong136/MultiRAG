"""Internal assistant metadata never reaches completion providers."""

from copy import deepcopy
from typing import Any

import pytest

from core.llm.chat import Base, LiteLLMBase


@pytest.mark.parametrize("model_class", [Base, LiteLLMBase])
def test_clean_conf_drops_internal_metadata_and_keeps_zero_extensions(model_class: type[Base] | type[LiteLLMBase]) -> None:
    model = model_class.__new__(model_class)
    model.model_name = "fixture"
    if isinstance(model, LiteLLMBase):
        model.provider = "OpenAI"
    generation: dict[str, Any] = {
        "temperature": 0,
        "top_p": 0,
        "presence_penalty": 0,
        "frequency_penalty": 0,
        "seed": 0,
        "tools": [{"type": "function", "function": {"name": "search"}}],
        "response_format": {"type": "json_object"},
    }
    conf = {**deepcopy(generation), "model_type": "chat", "llm_id": "internal", "parameter": "internal", "icon": "internal", "foo": "internal", "max_tokens": 10}
    original = deepcopy(conf)
    assert model._clean_conf(conf) == generation
    assert conf == original


def test_litellm_preserves_reasoning_and_minimax_query_options() -> None:
    model = LiteLLMBase.__new__(LiteLLMBase)
    model.model_name, model.provider = "fixture", "MiniMax"
    conf = {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "extra_body": {"enabled": True},
        "extra_query": {"GroupId": "fixture"},
        "extra_headers": {"X-Fixture": "value"},
        "model_type": "chat",
    }
    original = deepcopy(conf)
    assert model._clean_conf(conf) == {key: value for key, value in conf.items() if key != "model_type"}
    assert conf == original
