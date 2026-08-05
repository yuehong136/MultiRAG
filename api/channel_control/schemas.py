"""Strongly typed API and service contracts for chat channel management."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.channel_providers import provider_specs
from api.channel_providers.feishu import (
    FeishuConfigInput,
    FeishuConfigPatch,
    FeishuCredentialInput,
    FeishuCredentialPatch,
)
from api.channel_providers.spec import ProviderCapabilities

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

ChannelProvider = Literal["feishu"]
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
    config: FeishuConfigInput = Field(default_factory=FeishuConfigInput)
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
    config: FeishuConfigPatch | None = None
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
    channel: ChannelProvider
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
    provider: ChannelProvider
    display_name: str
    capabilities: ProviderCapabilities
    config_schema: dict[str, Any]


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
            config_schema=spec.config_model.model_json_schema(),
        )
        for spec in provider_specs()
    ]
