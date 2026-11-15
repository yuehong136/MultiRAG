"""The only gateway module that reads model configuration.

It asks ``model_provider_service`` for a registered model's connection and
turns it into the upstream address a relay needs. The address rules mirror
what the ``core/llm`` drivers of the same providers do today: the same root
URL (for self-hosted servers, versioned by ``ensure_api_version``) and the
``___`` suffix removed from the model name.
``tests/unit/test_model_gateway_catalog.py`` pins that equivalence, so a
driver change that moves the address shows up there.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.db.services import model_provider_service
from common.url_utils import ensure_api_version


@dataclass(frozen=True, slots=True)
class _AddressRule:
    """How one provider's stored base URL becomes the versioned upstream root."""

    # Used when the registration leaves the base URL empty.
    default_base_url: str = ""
    # Self-hosted servers are often registered without ``/v1``; their drivers add it.
    ensure_version: bool = True


# Providers that speak the OpenAI ``/v1/...`` protocol. Others are added one by
# one together with their address rule and a contract test.
_RELAY_RULES: dict[str, _AddressRule] = {
    "VLLM": _AddressRule(),
    "OpenAI-API-Compatible": _AddressRule(),
    "Xinference": _AddressRule(),
    "LocalAI": _AddressRule(),
    "LM-Studio": _AddressRule(),
    # The driver uses the configured base URL verbatim and has a default.
    "OpenAI": _AddressRule(default_base_url="https://api.openai.com/v1", ensure_version=False),
}
RELAY_FACTORIES: frozenset[str] = frozenset(_RELAY_RULES)
_ENDPOINT_PATHS: dict[str, str] = {"chat": "chat/completions", "embedding": "embeddings"}
SUPPORTED_MODEL_TYPES: frozenset[str] = frozenset(_ENDPOINT_PATHS)
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

    model_id: int
    factory: str
    model_name: str
    base_url: str
    api_key: str = field(repr=False)

    def endpoint_url(self, model_type: str) -> str:
        return f"{self.base_url}/{_ENDPOINT_PATHS[model_type]}"

    def authorization(self) -> str | None:
        return None if self.api_key in _NO_CREDENTIAL else f"Bearer {self.api_key}"


def upstream_from_config(config: Mapping[str, Any], model_id: int) -> UpstreamTarget:
    """Apply the relay address rules to a resolved model configuration."""
    factory = str(config.get("llm_factory") or "")
    rule = _RELAY_RULES.get(factory)
    if rule is None:
        raise ModelUnavailable("provider_not_supported")
    configured = str(config.get("api_base") or "").strip() or rule.default_base_url
    base_url = ensure_api_version(configured) if rule.ensure_version else configured.rstrip("/")
    if not base_url:
        raise ModelUnavailable("missing_base_url")
    model_name = str(config.get("llm_name") or "").split("___")[0]
    if not model_name:
        raise ModelUnavailable("missing_model_name")
    return UpstreamTarget(model_id=model_id, factory=factory, model_name=model_name, base_url=base_url, api_key=str(config.get("api_key") or ""))


async def resolve_upstream(db: AsyncSession, tenant_id: str, model_id: int, model_type: str) -> UpstreamTarget:
    """Resolve an owner's registered model for one capability, or raise ModelUnavailable."""
    if model_type not in SUPPORTED_MODEL_TYPES:
        raise ModelUnavailable("endpoint_not_supported")
    try:
        config = await model_provider_service.resolve_config(db, tenant_id, model_id, model_type)
    except LookupError as error:
        raise ModelUnavailable("model_not_available") from error
    return upstream_from_config(config, model_id)


async def describe_models(db: AsyncSession, tenant_id: str) -> dict[int, dict[str, Any]]:
    """Display metadata of the owner's registered models, keyed by numeric model id."""
    described: dict[int, dict[str, Any]] = {}
    for row, model, instance, provider in await model_provider_service.models_for_owner(db, tenant_id):
        if row.id is None:
            continue
        projection = model_provider_service.model_projection(row, model, instance, provider)
        described[row.id] = {
            "model_name": model.model_name,
            "provider": provider.provider_name,
            "instance": instance.instance_name,
            "model_types": list(model.model_types),
            "available": projection["effective_status"] == "active",
            "relayable": row.llm_factory in RELAY_FACTORIES,
        }
    return described
