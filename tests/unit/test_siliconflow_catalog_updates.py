"""The two catalogs retain their own token units and Kimi policy behavior."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NAMES = ["Pro/deepseek-ai/DeepSeek-V4-Pro", "Pro/deepseek-ai/DeepSeek-V4-Flash", "Pro/moonshotai/Kimi-K2.6", "Pro/zai-org/GLM-5.1"]


def test_four_models_have_exact_identifiers_contexts_and_capabilities() -> None:
    native = json.loads((ROOT / "configs/models/siliconflow.json").read_text())
    python = json.loads((ROOT / "configs/llm_factories.json").read_text())
    factory = next(item for item in python["factory_llm_infos"] if item["name"] == "SILICONFLOW")
    for index, name in enumerate(NAMES):
        native_models = [model for model in native["models"] if model["name"] == name]
        python_models = [model for model in factory["llm"] if model["llm_name"] == name]
        assert len(native_models) == len(python_models) == 1
        assert native_models[0]["max_tokens"] == [1048576, 1048576, 262144, 204800][index]
        assert python_models[0]["max_tokens"] == [1000000, 1000000, 262000, 205000][index]
        assert native_models[0]["model_types"] == (["chat", "vision"] if index == 2 else ["chat"])
        assert python_models[0]["mdl_type"] == "chat" and python_models[0]["is_tools"] is True
        assert ("IMAGE2TEXT" in python_models[0]["tags"]) is (index == 2)


@pytest.mark.parametrize("reasoning,temperature", [(False, 0.6), (True, 1.0)])
def test_kimi_k26_matches_family_generation_policy(reasoning: bool, temperature: float) -> None:
    from core.llm import SupportedLiteLLMProvider
    from core.llm.chat import _apply_model_family_policies

    config, _ = _apply_model_family_policies(
        "Pro/moonshotai/Kimi-K2.6", gen_conf={"reasoning": reasoning, "temperature": 0.2}, request_kwargs={}, backend="litellm", provider=SupportedLiteLLMProvider.SILICONFLOW
    )
    assert config["temperature"] == temperature and config["top_p"] == 0.95
    assert config["thinking"] == {"type": "enabled" if reasoning else "disabled"}
