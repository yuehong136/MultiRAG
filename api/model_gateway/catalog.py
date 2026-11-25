"""The only gateway module that reads model configuration.

It asks ``model_provider_service`` for a registered model's connection and
turns it into the upstream address a relay needs. The address rules mirror
what the ``core/llm`` drivers of the same providers do today: the same root
URL (for self-hosted servers, versioned by ``ensure_api_version``) and the
``___`` suffix removed from the model name.
``tests/unit/test_model_gateway_catalog.py`` pins that equivalence, so a
driver change that moves the address shows up there.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession

from api.db.services import model_provider_service
from common.url_utils import ensure_api_version

_DASHSCOPE_COMPATIBLE_ROOT = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _dashscope_compatible_root(configured: str) -> str:
    """DashScope's OpenAI-compatible root for a Tongyi-Qianwen registration.

    The chat driver sends a compatible root to the OpenAI protocol as is, and
    uses the same default when the registration is empty. The embedding driver
    uses the native ``/api/v1`` API of the same host instead, so a native root
    on an Alibaba Cloud host is moved to that host's compatible root. A native
    root elsewhere (a private proxy) has no known compatible root.
    """
    base_url = configured.rstrip("/") or _DASHSCOPE_COMPATIBLE_ROOT
    parsed = urlsplit(base_url)
    host = parsed.hostname or ""
    if host.endswith(".aliyuncs.com") and (parsed.path == "" or parsed.path.endswith("/api/v1")):
        return urlunsplit(parsed._replace(path=parsed.path.removesuffix("/api/v1") + "/compatible-mode/v1"))
    if parsed.path.endswith("/api/v1"):
        raise ModelUnavailable("unsupported_base_url")
    return base_url


@dataclass(frozen=True, slots=True)
class _AddressRule:
    """How one provider's stored base URL becomes the versioned upstream root."""

    # Used when the registration leaves the base URL empty.
    default_base_url: str = ""
    # Self-hosted servers are often registered without ``/v1``; their drivers add it.
    ensure_version: bool = True
    # A provider-specific mapping of the stored base URL; replaces the two fields above.
    root: Callable[[str], str] | None = None
    # Capabilities whose driver uses this root. A provider whose embedding driver
    # is registered with another address shape is relayed for chat only.
    model_types: frozenset[str] = frozenset({"chat", "embedding"})


_CHAT = frozenset({"chat"})


def _hosted(default_base_url: str, *, embedding: bool = False) -> _AddressRule:
    """A hosted OpenAI-protocol provider: its driver uses the stored base URL
    verbatim and falls back to the provider's public root."""
    return _AddressRule(default_base_url=default_base_url, ensure_version=False, model_types=frozenset({"chat", "embedding"}) if embedding else _CHAT)


# Providers reachable over the OpenAI ``/v1/...`` protocol. Others are added one
# by one together with their address rule and a contract test.
_RELAY_RULES: dict[str, _AddressRule] = {
    "VLLM": _AddressRule(),
    "OpenAI-API-Compatible": _AddressRule(),
    "Xinference": _AddressRule(),
    "LocalAI": _AddressRule(),
    "LM-Studio": _AddressRule(),
    # The driver uses the configured base URL verbatim and has a default.
    "OpenAI": _AddressRule(default_base_url="https://api.openai.com/v1", ensure_version=False),
    "Tongyi-Qianwen": _AddressRule(root=_dashscope_compatible_root),
    # Hosted providers the chat drivers reach over the OpenAI protocol. The
    # defaults repeat ``core.llm.FACTORY_DEFAULT_BASE_URL`` and the constructor
    # defaults of the drivers; the contract test compares them one by one.
    # Embeddings are relayed where the embedding driver is ``OpenAIEmbed`` on
    # the same root.
    "DeepSeek": _hosted("https://api.deepseek.com/v1"),
    "Moonshot": _hosted("https://api.moonshot.cn/v1"),
    "ZHIPU-AI": _hosted("https://open.bigmodel.cn/api/paas/v4"),
    "SILICONFLOW": _hosted("https://api.siliconflow.cn/v1"),
    "StepFun": _hosted("https://api.stepfun.com/v1"),
    "01.AI": _hosted("https://api.lingyiwanwu.com/v1"),
    "BaiduYiyan": _hosted("https://qianfan.baidubce.com/v2"),
    "Tencent Hunyuan": _hosted("https://api.hunyuan.cloud.tencent.com/v1"),
    "PPIO": _hosted("https://api.ppinfra.com/v3/openai"),
    "NovitaAI": _hosted("https://api.novita.ai/v3/openai"),
    "GiteeAI": _hosted("https://ai.gitee.com/v1"),
    "302.AI": _hosted("https://api.302.ai/v1"),
    "Jiekou.AI": _hosted("https://api.jiekou.ai/openai"),
    "LongCat": _hosted("https://api.longcat.chat/openai"),
    "n1n": _hosted("https://api.n1n.ai/v1"),
    "Avian": _hosted("https://api.avian.io/v1"),
    "CometAPI": _hosted("https://api.cometapi.com/v1", embedding=True),
    "DeerAPI": _hosted("https://api.deerapi.com/v1", embedding=True),
    "PerfXCloud": _hosted("https://cloud.perfxlab.cn/v1", embedding=True),
    "Upstage": _hosted("https://api.upstage.ai/v1/solar", embedding=True),
    "Astraflow": _hosted("https://api-us-ca.umodelverse.ai/v1", embedding=True),
    "Astraflow-CN": _hosted("https://api.modelverse.cn/v1", embedding=True),
    "FuturMix": _hosted("https://futurmix.ai/v1", embedding=True),
}
RELAY_FACTORIES: frozenset[str] = frozenset(_RELAY_RULES)
_ENDPOINT_PATHS: dict[str, str] = {"chat": "chat/completions", "embedding": "embeddings"}
SUPPORTED_MODEL_TYPES: frozenset[str] = frozenset(_ENDPOINT_PATHS)


def relay_types(factory: str) -> frozenset[str]:
    """The capabilities the gateway can relay for a provider; empty when it is not in the rules."""
    rule = _RELAY_RULES.get(factory)
    return rule.model_types & SUPPORTED_MODEL_TYPES if rule is not None else frozenset()


# Drivers treat these stored values as "no credential".
_NO_CREDENTIAL = frozenset({"", "x"})


class ModelUnavailable(Exception):
    """The registered model cannot be served through the gateway right now."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class UpstreamTarget:
    """Where and how to reach one registered model."""

    model_id: str
    factory: str
    model_name: str
    base_url: str
    api_key: str = field(repr=False)

    def endpoint_url(self, model_type: str) -> str:
        return f"{self.base_url}/{_ENDPOINT_PATHS[model_type]}"

    def authorization(self) -> str | None:
        return None if self.api_key in _NO_CREDENTIAL else f"Bearer {self.api_key}"


def upstream_from_config(config: Mapping[str, Any], model_id: str, model_type: str | None = None) -> UpstreamTarget:
    """Apply the relay address rules to a resolved model configuration."""
    factory = str(config.get("llm_factory") or "")
    rule = _RELAY_RULES.get(factory)
    if rule is None:
        raise ModelUnavailable("provider_not_supported")
    if model_type is not None and model_type not in rule.model_types:
        raise ModelUnavailable("endpoint_not_supported")
    configured = str(config.get("api_base") or "").strip()
    if rule.root is not None:
        base_url = rule.root(configured)
    else:
        configured = configured or rule.default_base_url
        base_url = ensure_api_version(configured) if rule.ensure_version else configured.rstrip("/")
    if not base_url:
        raise ModelUnavailable("missing_base_url")
    model_name = str(config.get("llm_name") or "").split("___")[0]
    if not model_name:
        raise ModelUnavailable("missing_model_name")
    return UpstreamTarget(model_id=model_id, factory=factory, model_name=model_name, base_url=base_url, api_key=str(config.get("api_key") or ""))


async def resolve_upstream(db: AsyncSession, tenant_id: str, model_id: str, model_type: str) -> UpstreamTarget:
    """Resolve an owner's registered model for one capability, or raise ModelUnavailable."""
    if model_type not in SUPPORTED_MODEL_TYPES:
        raise ModelUnavailable("endpoint_not_supported")
    try:
        config = await model_provider_service.resolve_config(db, tenant_id, model_id, model_type)
    except LookupError as error:
        raise ModelUnavailable("model_not_available") from error
    return upstream_from_config(config, model_id, model_type)


@dataclass(frozen=True, slots=True)
class RegisteredModel:
    """One registered model as the gateway catalog shows it."""

    model_id: str
    registration_order: int
    model_name: str
    provider: str
    instance_id: str
    instance_name: str
    model_types: tuple[str, ...]
    # The model, its connection and its provider are all enabled.
    available: bool
    # The provider is in the relay rules.
    relayable: bool
    # Registration time in milliseconds, for ``/v1/models``.
    created: int
    # The capabilities the provider's relay rule covers.
    relay_types: frozenset[str] = SUPPORTED_MODEL_TYPES


@dataclass(frozen=True, slots=True)
class Connection:
    """One provider instance (connection) of the owner, with or without models."""

    instance_id: str
    instance_name: str
    provider: str


async def registered_models(db: AsyncSession, tenant_id: str) -> list[RegisteredModel]:
    """Every model the owner registered, in model management's order."""
    models = []
    for model, instance, provider in await model_provider_service.models_for_owner(db, tenant_id):
        projection = model_provider_service.model_projection(model, instance, provider)
        models.append(
            RegisteredModel(
                model_id=model.id,
                registration_order=model.registration_order,
                # The upstream name: the registration suffix is not part of it.
                model_name=model.model_name.split("___")[0],
                provider=provider.provider_name,
                instance_id=instance.id,
                instance_name=instance.instance_name,
                model_types=tuple(model.model_types),
                available=projection["effective_status"] == "active",
                relayable=provider.provider_name in RELAY_FACTORIES,
                created=int(model.create_time or 0),
                relay_types=relay_types(provider.provider_name),
            )
        )
    return models


async def connections(db: AsyncSession, tenant_id: str) -> list[Connection]:
    """Every connection of the owner, including those without models."""
    return [
        Connection(instance_id=instance.id, instance_name=instance.instance_name, provider=provider.provider_name)
        for instance, provider in await model_provider_service.instances_for_owner(db, tenant_id)
    ]
