"""Relay address rules, and their equivalence with the model drivers.

The gateway does not call ``core/llm``; it must nevertheless reach the same
address with the same model name as the driver of the same provider would.
"""

import pytest

from api.model_gateway.catalog import RELAY_FACTORIES, ModelUnavailable, upstream_from_config

_BASE_URLS = ["http://models.internal:8000", "http://models.internal:8000/", "http://models.internal:8000/v1", "http://models.internal:8000/serving/v1/", "http://models.internal:8000/proxy"]
_OPENAI_BASE_URLS = ["", "https://api.openai.com/v1", "https://api.openai.com/v1/", "https://relay.internal/openai/v1", "https://relay.internal/custom"]
# OpenAI has its own rule (default URL, base used verbatim) and its own driver shape.
_SELF_HOSTED = sorted(RELAY_FACTORIES - {"OpenAI"})
# Providers whose driver strips the ``___<suffix>`` appended at registration time.
_CHAT_STRIPS_SUFFIX = {"VLLM", "OpenAI-API-Compatible"}
_EMBEDDING_STRIPS_SUFFIX = {"VLLM", "OpenAI-API-Compatible", "LocalAI"}


def _config(factory: str, base_url: str, name: str = "served-model", key: str = "upstream-key") -> dict[str, str]:
    return {"llm_factory": factory, "api_base": base_url, "llm_name": name, "api_key": key}


def _driver_address(driver: object) -> tuple[str, str]:
    return str(driver.client.base_url).rstrip("/"), driver.model_name


def test_target_carries_versioned_root_and_endpoint_paths():
    target = upstream_from_config(_config("VLLM", "http://models.internal:8000/"), 42)

    assert target.model_id == 42
    assert target.base_url == "http://models.internal:8000/v1"
    assert target.endpoint_url("chat") == "http://models.internal:8000/v1/chat/completions"
    assert target.endpoint_url("embedding") == "http://models.internal:8000/v1/embeddings"


def test_registration_suffix_is_removed_from_the_model_name():
    assert upstream_from_config(_config("VLLM", "http://h/v1", "qwen___VLLM"), 1).model_name == "qwen"


@pytest.mark.parametrize(("key", "expected"), [("upstream-key", "Bearer upstream-key"), ("", None), ("x", None)])
def test_placeholder_credentials_send_no_authorization(key, expected):
    assert upstream_from_config(_config("Xinference", "http://h", key=key), 1).authorization() == expected


def test_credential_is_not_printed():
    assert "upstream-key" not in repr(upstream_from_config(_config("VLLM", "http://h"), 1))


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (_config("Anthropic", "https://api.anthropic.com"), "provider_not_supported"),
        (_config("Tongyi-Qianwen", "https://dashscope.aliyuncs.com/compatible-mode/v1"), "provider_not_supported"),
        (_config("VLLM", ""), "missing_base_url"),
        (_config("VLLM", "http://h", name=""), "missing_model_name"),
        (_config("VLLM", "http://h", name="___VLLM"), "missing_model_name"),
    ],
)
def test_models_outside_the_relay_rules_are_unavailable(config, reason):
    with pytest.raises(ModelUnavailable) as raised:
        upstream_from_config(config, 1)

    assert raised.value.reason == reason


@pytest.mark.parametrize("factory", _SELF_HOSTED)
@pytest.mark.parametrize("api_base", _BASE_URLS)
def test_chat_address_matches_the_chat_driver(factory, api_base):
    from core.llm import ChatModel

    driver = ChatModel[factory](key="upstream-key", model_name="served-model", base_url=api_base)
    target = upstream_from_config(_config(factory, api_base), 1)

    assert (target.base_url, target.model_name) == _driver_address(driver)


@pytest.mark.parametrize("factory", _SELF_HOSTED)
@pytest.mark.parametrize("api_base", _BASE_URLS)
def test_embedding_address_matches_the_embedding_driver(factory, api_base):
    from core.llm import EmbeddingModel

    driver = EmbeddingModel[factory](key="upstream-key", model_name="served-model", base_url=api_base)
    target = upstream_from_config(_config(factory, api_base), 1)

    assert (target.base_url, target.model_name) == _driver_address(driver)


@pytest.mark.parametrize("factory", sorted(_CHAT_STRIPS_SUFFIX))
def test_suffixed_chat_name_matches_the_chat_driver(factory):
    from core.llm import ChatModel

    name = f"served-model___{factory}"
    driver = ChatModel[factory](key="upstream-key", model_name=name, base_url="http://models.internal:8000")

    assert upstream_from_config(_config(factory, "http://models.internal:8000", name), 1).model_name == driver.model_name == "served-model"


@pytest.mark.parametrize("factory", sorted(_EMBEDDING_STRIPS_SUFFIX))
def test_suffixed_embedding_name_matches_the_embedding_driver(factory):
    from core.llm import EmbeddingModel

    name = f"served-model___{factory}"
    driver = EmbeddingModel[factory](key="upstream-key", model_name=name, base_url="http://models.internal:8000")

    assert upstream_from_config(_config(factory, "http://models.internal:8000", name), 1).model_name == driver.model_name == "served-model"


def test_openai_registration_without_base_url_uses_the_default_endpoint():
    target = upstream_from_config(_config("OpenAI", "", "gpt-served"), 7)

    assert target.endpoint_url("chat") == "https://api.openai.com/v1/chat/completions"
    assert target.endpoint_url("embedding") == "https://api.openai.com/v1/embeddings"
    assert target.authorization() == "Bearer upstream-key"


@pytest.mark.parametrize("api_base", _OPENAI_BASE_URLS)
def test_openai_chat_address_matches_the_chat_driver(api_base):
    from core.llm import ChatModel

    driver = ChatModel["OpenAI"](key="upstream-key", model_name="gpt-served", base_url=api_base, provider="OpenAI")
    target = upstream_from_config(_config("OpenAI", api_base, "gpt-served"), 1)

    # The driver hands this base to LiteLLM, which appends /chat/completions.
    assert (target.base_url, target.model_name) == (driver.base_url, driver.model_name.removeprefix(driver.prefix))
    assert driver.prefix == "openai/"


@pytest.mark.parametrize("api_base", _OPENAI_BASE_URLS)
def test_openai_embedding_address_matches_the_embedding_driver(api_base):
    from core.llm import EmbeddingModel

    driver = EmbeddingModel["OpenAI"](key="upstream-key", model_name="embedding-served", base_url=api_base)
    target = upstream_from_config(_config("OpenAI", api_base, "embedding-served"), 1)

    assert (target.base_url, target.model_name) == _driver_address(driver)
