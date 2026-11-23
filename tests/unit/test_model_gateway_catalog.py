"""Relay address rules, and their equivalence with the model drivers.

The gateway does not call ``core/llm``; it must nevertheless reach the same
address with the same model name as the driver of the same provider would.
"""

from urllib.parse import urlsplit

import pytest

from api.model_gateway.catalog import RELAY_FACTORIES, ModelUnavailable, upstream_from_config

_BASE_URLS = ["http://models.internal:8000", "http://models.internal:8000/", "http://models.internal:8000/v1", "http://models.internal:8000/serving/v1/", "http://models.internal:8000/proxy"]
_OPENAI_BASE_URLS = ["", "https://api.openai.com/v1", "https://api.openai.com/v1/", "https://relay.internal/openai/v1", "https://relay.internal/custom"]
# OpenAI and Tongyi-Qianwen have their own rules and driver shapes, tested below.
_SELF_HOSTED = sorted(RELAY_FACTORIES - {"OpenAI", "Tongyi-Qianwen"})
# Providers whose driver strips the ``___<suffix>`` appended at registration time.
_CHAT_STRIPS_SUFFIX = {"VLLM", "OpenAI-API-Compatible"}
_EMBEDDING_STRIPS_SUFFIX = {"VLLM", "OpenAI-API-Compatible", "LocalAI"}


MODEL_ID = "a" * 32


def _config(factory: str, base_url: str, name: str = "served-model", key: str = "upstream-key") -> dict[str, str]:
    return {"llm_factory": factory, "api_base": base_url, "llm_name": name, "api_key": key}


def _driver_address(driver: object) -> tuple[str, str]:
    return str(driver.client.base_url).rstrip("/"), driver.model_name


def test_target_carries_versioned_root_and_endpoint_paths():
    target = upstream_from_config(_config("VLLM", "http://models.internal:8000/"), MODEL_ID)

    assert target.model_id == MODEL_ID
    assert target.base_url == "http://models.internal:8000/v1"
    assert target.endpoint_url("chat") == "http://models.internal:8000/v1/chat/completions"
    assert target.endpoint_url("embedding") == "http://models.internal:8000/v1/embeddings"


def test_registration_suffix_is_removed_from_the_model_name():
    assert upstream_from_config(_config("VLLM", "http://h/v1", "qwen___VLLM"), MODEL_ID).model_name == "qwen"


@pytest.mark.parametrize(("key", "expected"), [("upstream-key", "Bearer upstream-key"), ("", None), ("x", None)])
def test_placeholder_credentials_send_no_authorization(key, expected):
    assert upstream_from_config(_config("Xinference", "http://h", key=key), MODEL_ID).authorization() == expected


def test_credential_is_not_printed():
    assert "upstream-key" not in repr(upstream_from_config(_config("VLLM", "http://h"), MODEL_ID))


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (_config("Anthropic", "https://api.anthropic.com"), "provider_not_supported"),
        (_config("Gemini", ""), "provider_not_supported"),
        # A native-only DashScope proxy has no OpenAI-compatible root to relay to.
        (_config("Tongyi-Qianwen", "https://dashscope-proxy.internal/api/v1"), "unsupported_base_url"),
        (_config("VLLM", ""), "missing_base_url"),
        (_config("VLLM", "http://h", name=""), "missing_model_name"),
        (_config("VLLM", "http://h", name="___VLLM"), "missing_model_name"),
    ],
)
def test_models_outside_the_relay_rules_are_unavailable(config, reason):
    with pytest.raises(ModelUnavailable) as raised:
        upstream_from_config(config, MODEL_ID)

    assert raised.value.reason == reason


@pytest.mark.parametrize("factory", _SELF_HOSTED)
@pytest.mark.parametrize("api_base", _BASE_URLS)
def test_chat_address_matches_the_chat_driver(factory, api_base):
    from core.llm import ChatModel

    driver = ChatModel[factory](key="upstream-key", model_name="served-model", base_url=api_base)
    target = upstream_from_config(_config(factory, api_base), MODEL_ID)

    assert (target.base_url, target.model_name) == _driver_address(driver)


@pytest.mark.parametrize("factory", _SELF_HOSTED)
@pytest.mark.parametrize("api_base", _BASE_URLS)
def test_embedding_address_matches_the_embedding_driver(factory, api_base):
    from core.llm import EmbeddingModel

    driver = EmbeddingModel[factory](key="upstream-key", model_name="served-model", base_url=api_base)
    target = upstream_from_config(_config(factory, api_base), MODEL_ID)

    assert (target.base_url, target.model_name) == _driver_address(driver)


@pytest.mark.parametrize("factory", sorted(_CHAT_STRIPS_SUFFIX))
def test_suffixed_chat_name_matches_the_chat_driver(factory):
    from core.llm import ChatModel

    name = f"served-model___{factory}"
    driver = ChatModel[factory](key="upstream-key", model_name=name, base_url="http://models.internal:8000")

    assert upstream_from_config(_config(factory, "http://models.internal:8000", name), MODEL_ID).model_name == driver.model_name == "served-model"


@pytest.mark.parametrize("factory", sorted(_EMBEDDING_STRIPS_SUFFIX))
def test_suffixed_embedding_name_matches_the_embedding_driver(factory):
    from core.llm import EmbeddingModel

    name = f"served-model___{factory}"
    driver = EmbeddingModel[factory](key="upstream-key", model_name=name, base_url="http://models.internal:8000")

    assert upstream_from_config(_config(factory, "http://models.internal:8000", name), MODEL_ID).model_name == driver.model_name == "served-model"


def test_openai_registration_without_base_url_uses_the_default_endpoint():
    target = upstream_from_config(_config("OpenAI", "", "gpt-served"), MODEL_ID)

    assert target.endpoint_url("chat") == "https://api.openai.com/v1/chat/completions"
    assert target.endpoint_url("embedding") == "https://api.openai.com/v1/embeddings"
    assert target.authorization() == "Bearer upstream-key"


@pytest.mark.parametrize("api_base", _OPENAI_BASE_URLS)
def test_openai_chat_address_matches_the_chat_driver(api_base):
    from core.llm import ChatModel

    driver = ChatModel["OpenAI"](key="upstream-key", model_name="gpt-served", base_url=api_base, provider="OpenAI")
    target = upstream_from_config(_config("OpenAI", api_base, "gpt-served"), MODEL_ID)

    # The driver hands this base to LiteLLM, which appends /chat/completions.
    assert (target.base_url, target.model_name) == (driver.base_url, driver.model_name.removeprefix(driver.prefix))
    assert driver.prefix == "openai/"


@pytest.mark.parametrize("api_base", _OPENAI_BASE_URLS)
def test_openai_embedding_address_matches_the_embedding_driver(api_base):
    from core.llm import EmbeddingModel

    driver = EmbeddingModel["OpenAI"](key="upstream-key", model_name="embedding-served", base_url=api_base)
    target = upstream_from_config(_config("OpenAI", api_base, "embedding-served"), MODEL_ID)

    assert (target.base_url, target.model_name) == _driver_address(driver)


# Tongyi-Qianwen: both capabilities are relayed to DashScope's OpenAI-compatible root.
_DASHSCOPE = "https://dashscope.aliyuncs.com"
_TONGYI_COMPATIBLE_BASE_URLS = [
    "",
    f"{_DASHSCOPE}/compatible-mode/v1",
    f"{_DASHSCOPE}/compatible-mode/v1/",
    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "https://relay.internal/dashscope/compatible-mode/v1",
]
_TONGYI_NATIVE_BASE_URLS = [f"{_DASHSCOPE}/api/v1", "https://dashscope-intl.aliyuncs.com/api/v1/", "https://dashscope-us.aliyuncs.com/api/v1"]


@pytest.mark.parametrize(
    ("api_base", "root"),
    [
        ("", f"{_DASHSCOPE}/compatible-mode/v1"),
        (f"{_DASHSCOPE}/compatible-mode/v1/", f"{_DASHSCOPE}/compatible-mode/v1"),
        (_DASHSCOPE, f"{_DASHSCOPE}/compatible-mode/v1"),
        (f"{_DASHSCOPE}/api/v1", f"{_DASHSCOPE}/compatible-mode/v1"),
        ("https://dashscope-intl.aliyuncs.com/api/v1/", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        ("https://dashscope-us.aliyuncs.com/api/v1", "https://dashscope-us.aliyuncs.com/compatible-mode/v1"),
        ("https://relay.internal/dashscope/compatible-mode/v1", "https://relay.internal/dashscope/compatible-mode/v1"),
    ],
)
def test_tongyi_registrations_relay_to_the_compatible_root(api_base, root):
    target = upstream_from_config(_config("Tongyi-Qianwen", api_base, "qwen-flash"), MODEL_ID)

    assert target.base_url == root
    assert target.endpoint_url("chat") == f"{root}/chat/completions"
    assert target.endpoint_url("embedding") == f"{root}/embeddings"
    assert target.model_name == "qwen-flash"


@pytest.mark.parametrize("api_base", _TONGYI_COMPATIBLE_BASE_URLS)
def test_tongyi_chat_address_matches_the_chat_driver(api_base):
    from core.llm import ChatModel

    driver = ChatModel["Tongyi-Qianwen"](key="upstream-key", model_name="qwen-flash", base_url=api_base, provider="Tongyi-Qianwen")
    target = upstream_from_config(_config("Tongyi-Qianwen", api_base, "qwen-flash"), MODEL_ID)

    # On a compatible root the driver speaks the OpenAI protocol through LiteLLM.
    assert driver.prefix == "openai/"
    assert (target.base_url, target.model_name) == (driver.base_url, driver.model_name.removeprefix(driver.prefix))


@pytest.mark.parametrize("api_base", _TONGYI_NATIVE_BASE_URLS)
def test_tongyi_native_chat_registration_is_relayed_on_the_same_host(api_base):
    from core.llm import ChatModel

    driver = ChatModel["Tongyi-Qianwen"](key="upstream-key", model_name="qwen-flash", base_url=api_base, provider="Tongyi-Qianwen")
    target = upstream_from_config(_config("Tongyi-Qianwen", api_base, "qwen-flash"), MODEL_ID)

    # The driver uses the native protocol here; the gateway uses the same host's compatible root.
    assert driver.prefix == "dashscope/"
    assert urlsplit(target.base_url).hostname == urlsplit(driver.base_url).hostname
    assert target.model_name == driver.model_name.removeprefix(driver.prefix)


@pytest.mark.parametrize("api_base", [*_TONGYI_COMPATIBLE_BASE_URLS[:4], _DASHSCOPE, *_TONGYI_NATIVE_BASE_URLS])
def test_tongyi_embedding_is_relayed_on_the_embedding_driver_host(api_base):
    from core.llm import EmbeddingModel

    driver = EmbeddingModel["Tongyi-Qianwen"](key="upstream-key", model_name="text-embedding-v4", base_url=api_base)
    target = upstream_from_config(_config("Tongyi-Qianwen", api_base, "text-embedding-v4"), MODEL_ID)

    # The driver calls the native API, by default on the SDK's own host.
    native = driver._dashscope_http_api_url or f"{_DASHSCOPE}/api/v1"
    assert urlsplit(target.base_url).hostname == urlsplit(native).hostname
    assert target.model_name == driver.model_name
