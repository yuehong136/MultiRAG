"""Astraflow（全球 / 中国双端点）provider 的注册完整性。

新增一个 OpenAI 兼容 provider 要同时落到五处：模型类（靠 ``_FACTORY_NAME`` 自动
进 ChatModel / EmbeddingModel 注册表）、LiteLLM 的三张表（枚举 / 默认 base_url /
provider 前缀）、以及 llm_factories.json。漏掉任何一处都不会报错，只会在用户选到
这个厂商时才发作，所以逐处钉住。
"""

import json
import pathlib

import pytest

from core.llm import FACTORY_DEFAULT_BASE_URL, LITELLM_PROVIDER_PREFIX, ChatModel, EmbeddingModel, SupportedLiteLLMProvider

_FACTORIES = json.loads((pathlib.Path(__file__).resolve().parents[2] / "configs" / "llm_factories.json").read_text())

_VARIANTS = [
    ("Astraflow", SupportedLiteLLMProvider.Astraflow, "https://api-us-ca.umodelverse.ai/v1"),
    ("Astraflow-CN", SupportedLiteLLMProvider.Astraflow_CN, "https://api.modelverse.cn/v1"),
]


@pytest.mark.parametrize(("factory_name", "provider", "default_url"), _VARIANTS)
def test_model_classes_are_registered(factory_name, provider, default_url):
    assert factory_name in ChatModel
    assert factory_name in EmbeddingModel


@pytest.mark.parametrize(("factory_name", "provider", "default_url"), _VARIANTS)
def test_litellm_tables_cover_the_provider(factory_name, provider, default_url):
    assert provider.value == factory_name
    assert FACTORY_DEFAULT_BASE_URL[provider] == default_url
    # 聚合平台走 OpenAI 兼容协议
    assert LITELLM_PROVIDER_PREFIX[provider] == "openai/"


@pytest.mark.parametrize(("factory_name", "provider", "default_url"), _VARIANTS)
def test_blank_base_url_falls_back_to_the_default(factory_name, provider, default_url):
    """配置里 base_url 留空时不能拼出 ``None/chat/completions``。"""
    chat = ChatModel[factory_name]("key", "some-model", base_url="")
    assert default_url in str(chat.client.base_url)
    assert default_url in str(chat.async_client.base_url)

    embed = EmbeddingModel[factory_name]("key", "some-model", base_url="")
    assert default_url in str(embed.client.base_url)


@pytest.mark.parametrize(("factory_name", "provider", "default_url"), _VARIANTS)
def test_factory_entry_uses_our_model_type_key(factory_name, provider, default_url):
    """我方 llm_factories 用 mdl_type，上游用 model_type——照搬会让整个厂商的模型列表读不出类型。"""
    entries = [f for f in _FACTORIES["factory_llm_infos"] if f["name"] == factory_name]
    assert len(entries) == 1, f"{factory_name} 应在 llm_factories.json 中恰好出现一次"

    entry = entries[0]
    assert entry["url"] == default_url
    assert entry["llm"], "厂商条目不能没有模型"
    for model in entry["llm"]:
        assert "mdl_type" in model, f"{model.get('llm_name')} 缺 mdl_type"
        assert "model_type" not in model, f"{model.get('llm_name')} 残留上游的 model_type 键"
