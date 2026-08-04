"""Strongly typed API and service contracts for chat channel management."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

ChannelProvider = Literal["feishu"]
ChannelTargetType = Literal["multirag.canvas_agent", "multirag.dialog"]
SUPPORTED_TARGET_TYPES: frozenset[str] = frozenset({"multirag.canvas_agent", "multirag.dialog"})


class FeishuCredentialInput(BaseModel):
    """Write-only Feishu credentials accepted from the management UI."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    app_id: str | None = Field(default=None, min_length=1, max_length=128)
    app_secret: SecretStr | None = Field(
        default=None,
        json_schema_extra={"writeOnly": True},
    )


class FeishuConfigInput(BaseModel):
    """Public Feishu connection settings plus an optional write-only secret."""

    model_config = ConfigDict(extra="forbid")

    credential: FeishuCredentialInput = Field(default_factory=FeishuCredentialInput)
    domain: Literal["feishu", "lark"] = "feishu"
    allowed_open_ids: list[str] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="before")
    @classmethod
    def accept_upstream_credential_domain(cls, value: Any) -> Any:
        """Accept RAGFlow's nested domain while keeping our public canonical shape."""

        if not isinstance(value, dict) or "domain" in value:
            return value
        credential = value.get("credential")
        if not isinstance(credential, dict) or "domain" not in credential:
            return value
        normalized = dict(value)
        normalized["domain"] = credential["domain"]
        return normalized


class FeishuCredentialPatch(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    app_id: str | None = Field(default=None, min_length=1, max_length=128)
    app_secret: SecretStr | None = Field(
        default=None,
        json_schema_extra={"writeOnly": True},
    )


class FeishuConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: FeishuCredentialPatch | None = None
    domain: Literal["feishu", "lark"] | None = None
    allowed_open_ids: list[str] | None = Field(default=None, max_length=1000)

    @model_validator(mode="before")
    @classmethod
    def accept_upstream_credential_domain(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "domain" in value:
            return value
        credential = value.get("credential")
        if not isinstance(credential, dict) or "domain" not in credential:
            return value
        normalized = dict(value)
        normalized["domain"] = credential["domain"]
        return normalized


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


class ProviderCapabilities(BaseModel):
    private_chat: bool
    group_chat: bool
    text: bool
    files: bool
    images: bool
    streaming_cards: bool


class ProviderManifest(BaseModel):
    provider: ChannelProvider
    display_name: str
    capabilities: ProviderCapabilities
    config_schema: dict[str, Any]


def provider_manifests() -> list[ProviderManifest]:
    """Return server-owned provider metadata used to render management forms."""

    return [
        ProviderManifest(
            provider="feishu",
            display_name="Feishu / Lark",
            capabilities=ProviderCapabilities(
                private_chat=True,
                group_chat=False,
                text=True,
                files=False,
                images=False,
                streaming_cards=False,
            ),
            config_schema=FeishuConfigInput.model_json_schema(),
        )
    ]
