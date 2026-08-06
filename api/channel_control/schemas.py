"""Strongly typed API and service contracts for chat channel management."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from api.channel_providers import is_registered, provider_names, provider_specs
from api.channel_providers.feishu import (
    FeishuConfigInput,
    FeishuConfigPatch,
    FeishuCredentialInput,
    FeishuCredentialPatch,
)
from api.channel_providers.spec import ProviderCapabilities, ProviderForm

# Re-exported: these models describe a provider, not the control plane, so they
# now live in api/channel_providers/. Importers that predate the move keep
# working.
__all__ = [
    "FeishuConfigInput",
    "FeishuConfigPatch",
    "FeishuCredentialInput",
    "FeishuCredentialPatch",
    "ProviderCapabilities",
]


def _registered_provider(name: str) -> str:
    """Fail closed on a provider nobody declared.

    Was ``Literal["feishu"]``, which meant adding a provider required editing
    the control plane's request schema -- one of the places that gets
    forgotten, and the reason "multi-provider" was true of the registry and
    false of the API. The registry is the single list now; this reads it.

    Only requests are validated this way. Responses keep a plain ``str``: a
    stored row must stay readable even if its provider is later unregistered,
    or a deregistration would make existing channels unfetchable rather than
    merely unstartable.
    """

    if not is_registered(name):
        raise ValueError(f"unknown channel provider: expected one of {', '.join(provider_names())}")
    return name


ChannelProvider = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_registered_provider)]
ChannelTargetType = Literal["multirag.canvas_agent", "multirag.dialog"]
SUPPORTED_TARGET_TYPES: frozenset[str] = frozenset({"multirag.canvas_agent", "multirag.dialog"})


class ChannelBindingUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_type: ChannelTargetType
    target_id: str = Field(min_length=1, max_length=32)
    target_revision_id: str | None = Field(default=None, min_length=1, max_length=32)
    policy: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = False

    @model_validator(mode="after")
    def validate_revision(self) -> ChannelBindingUpsertRequest:
        if self.target_type == "multirag.canvas_agent" and self.target_revision_id is None:
            raise ValueError("target_revision_id is required for multirag.canvas_agent")
        if self.target_type == "multirag.dialog" and self.target_revision_id is not None:
            raise ValueError("target_revision_id is not supported for multirag.dialog")
        return self


class ChannelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=128)
    channel: ChannelProvider
    # An open object here, parsed by the provider's own model in the service
    # layer (`validate_config`). Naming one provider's model at this level is
    # what made every other provider's credentials get silently dropped.
    config: dict[str, Any] = Field(default_factory=dict)
    chat_id: str | None = Field(default=None, min_length=1, max_length=32)
    binding: ChannelBindingUpsertRequest | None = None
    status: Literal[0, 1] = 0

    @model_validator(mode="after")
    def validate_compatibility_target(self) -> ChannelCreateRequest:
        if self.chat_id is None or self.binding is None:
            return self
        if self.binding.target_type != "multirag.dialog" or self.binding.target_id != self.chat_id:
            raise ValueError("chat_id and binding must refer to the same multirag.dialog target")
        return self


class ChannelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=128)
    # A PATCH body carries no provider name -- only the stored row knows which
    # one this is -- so the dispatch cannot happen here. See `validate_config`.
    config: dict[str, Any] | None = None
    chat_id: str | None = Field(default=None, min_length=1, max_length=32)
    binding: ChannelBindingUpsertRequest | None = None
    status: Literal[0, 1] | None = None

    @model_validator(mode="after")
    def validate_compatibility_target(self) -> ChannelUpdateRequest:
        if self.binding is None:
            return self
        if "chat_id" in self.model_fields_set:
            if self.chat_id is None or self.binding.target_type != "multirag.dialog" or self.binding.target_id != self.chat_id:
                raise ValueError("chat_id and binding must refer to the same multirag.dialog target")
        if self.status is not None and self.binding.enabled != (self.status == 1):
            raise ValueError("status and binding.enabled must agree")
        return self


class SecretStatus(BaseModel):
    configured: bool
    version: int | None = None


class ChannelBindingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    channel_id: str
    target_type: ChannelTargetType
    target_id: str
    target_revision_id: str | None
    policy: dict[str, Any]
    enabled: bool
    generation: int
    # Read-only hint resolved on read paths only, mirroring ``runtime`` below:
    # True once the bound Canvas release stops being the latest published one,
    # None when the target is not version-addressable or was not resolved.
    revision_stale: bool | None = None


class ChannelRuntimeResponse(BaseModel):
    binding_id: str | None
    desired_generation: int | None
    observed_generation: int
    state: str
    runner_id: str | None
    heartbeat_at: datetime | None
    connected_at: datetime | None
    last_error_code: str | None


class ChatChannelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    name: str
    channel: str
    config: dict[str, Any]
    chat_id: str | None
    status: int
    generation: int
    create_time: int | None
    update_time: int | None
    secret: SecretStatus
    binding: ChannelBindingResponse | None
    runtime: ChannelRuntimeResponse | None = None


class ProviderManifest(BaseModel):
    """What a client needs to render and validate one provider's form.

    Two derived artefacts, deliberately:

    - ``form`` is the render contract -- a flattened, ordered field list. The
      client maps each entry to a widget; it resolves no ``$ref`` and evaluates
      no JSON Schema keyword.
    - ``config_schema`` stays the validation/OpenAPI contract, generated by
      pydantic and never hand-written.

    They come from one ``ProviderSpec`` and a consistency test binds them, so
    the pair cannot drift into disagreeing about what a provider accepts.
    """

    provider: str
    display_name: str
    capabilities: ProviderCapabilities
    form: ProviderForm
    config_schema: dict[str, Any]
    # One line for a client listing providers nobody has connected yet. Server
    # owned so a provider gallery is not a second place to declare a provider.
    description: str = ""
    description_i18n_key: str | None = None


def provider_manifests() -> list[ProviderManifest]:
    """Server-owned provider metadata used to render management forms.

    Derived from the registry rather than written out by hand: a hand-written
    list is a second place a provider has to be declared, and the one that gets
    forgotten. Adding a provider is now a registry entry plus a spec module.
    """

    return [
        ProviderManifest(
            provider=spec.name,
            display_name=spec.display_name,
            capabilities=spec.capabilities,
            form=spec.form,
            config_schema=spec.config_model.model_json_schema(),
            description=spec.description,
            description_i18n_key=spec.description_i18n_key,
        )
        for spec in provider_specs()
    ]
