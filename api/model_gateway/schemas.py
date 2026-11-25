"""Request bodies of the model gateway management API."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from api.db.model_contract import ModelID

_MAX_TAGS = 16
_MAX_TAG_KEY = 64
_MAX_TAG_VALUE = 256
_MAX_ALLOWED_MODELS = 500
_MAX_ALLOWED_INSTANCES = 200

ModelAlias = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")]
KeyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Description = Annotated[str, StringConstraints(max_length=2000)]


def _valid_tags(tags: dict[str, str] | None) -> dict[str, str] | None:
    if tags is None:
        return None
    if len(tags) > _MAX_TAGS:
        raise ValueError(f"at most {_MAX_TAGS} tags are allowed")
    for name, value in tags.items():
        if not name or len(name) > _MAX_TAG_KEY or len(value) > _MAX_TAG_VALUE:
            raise ValueError(f"tag names are 1-{_MAX_TAG_KEY} characters and values at most {_MAX_TAG_VALUE}")
    return tags


def _future_instant(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("expires_at must include a time zone offset")
    if value <= datetime.now(UTC):
        raise ValueError("expires_at must be in the future")
    return value


Tags = Annotated[dict[str, str] | None, AfterValidator(_valid_tags)]
Expiry = Annotated[datetime | None, AfterValidator(_future_instant)]
# Registered model ids, and connection (instance) ids whose models are all allowed.
AllowedModelIds = Annotated[list[ModelID], Field(max_length=_MAX_ALLOWED_MODELS)]
AllowedInstanceIds = Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=64)]], Field(max_length=_MAX_ALLOWED_INSTANCES)]
# Limits are positive; leaving one out (or sending null) means unlimited.
RateLimit = Annotated[int, Field(gt=0, le=2_000_000_000)]
TokenQuota = Annotated[int, Field(gt=0, le=9_000_000_000_000_000)]
QuotaPeriod = Literal["day", "month"]
# LiteLLM's ``weight`` and ``order`` of a deployment; null returns to the default.
Weight = Annotated[int, Field(ge=1, le=1000)]
DeploymentOrder = Annotated[int, Field(ge=1, le=100)]
# USD per token under LiteLLM's field names; twelve decimals hold a hundredth of a cent per million tokens.
TokenCost = Annotated[Decimal, Field(ge=0, le=1, max_digits=13, decimal_places=12)]


class _Body(BaseModel):
    # ``model_id`` and ``model_type`` are domain names here, not pydantic internals.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class _Limits(_Body):
    rpm_limit: RateLimit | None = None
    tpm_limit: RateLimit | None = None
    max_parallel_requests: RateLimit | None = None


class _KeyLimits(_Limits):
    token_quota: TokenQuota | None = None
    quota_period: QuotaPeriod | None = None

    @model_validator(mode="after")
    def _quota_and_period_go_together(self) -> Self:
        if (self.token_quota is None) != (self.quota_period is None):
            raise ValueError("token_quota and quota_period must be set together")
        return self


class CatalogModelPatch(_Limits):
    """Gateway settings of one registered model; omitted fields stay as they are."""

    enabled: bool | None = None
    alias: ModelAlias | None = None
    preferred: bool | None = None
    description: Description | None = None
    # Share of traffic among deployments of one order (default 1), and which order is tried first.
    weight: Weight | None = None
    order: DeploymentOrder | None = None
    # The operator's price; null returns the model to LiteLLM's cost map.
    input_cost_per_token: TokenCost | None = None
    output_cost_per_token: TokenCost | None = None
    cache_read_input_token_cost: TokenCost | None = None

    @model_validator(mode="after")
    def _a_price_starts_with_its_input_rate(self) -> Self:
        sent = self.model_fields_set
        clears_input = "input_cost_per_token" in sent and self.input_cost_per_token is None
        sets_other = self.output_cost_per_token is not None or self.cache_read_input_token_cost is not None
        if sets_other and (clears_input or "input_cost_per_token" not in sent):
            raise ValueError("output_cost_per_token and cache_read_input_token_cost need input_cost_per_token in the same request")
        return self


class CatalogInstancePatch(_Body):
    enabled: bool


class GatewaySettingsPatch(_Body):
    new_models_enabled: bool


class GatewayKeyCreate(_KeyLimits):
    name: KeyName
    description: Description | None = None
    tags: Tags = None
    expires_at: Expiry = None
    allow_all_models: bool = False
    allowed_model_ids: AllowedModelIds = Field(default_factory=list)
    allowed_instance_ids: AllowedInstanceIds = Field(default_factory=list)


class GatewayKeyPatch(_KeyLimits):
    name: KeyName | None = None
    description: Description | None = None
    tags: Tags = None
    expires_at: Expiry = None
    allow_all_models: bool | None = None
    allowed_model_ids: AllowedModelIds | None = None
    allowed_instance_ids: AllowedInstanceIds | None = None
    status: Literal["active", "disabled"] | None = None
