"""Feishu / Lark provider specification.

The pydantic models moved here from ``api/channel_control/schemas.py``: they
describe the provider, not the control plane, and keeping them next to the
control plane is what made "add a provider" mean "edit the control plane".
``schemas.py`` re-exports them so existing importers keep working.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from api.channel_providers.spec import (
    FieldOption,
    FormField,
    ProviderCapabilities,
    ProviderForm,
    ProviderSpec,
)


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


_FORM = ProviderForm(
    fields=[
        FormField(
            path="credential.app_id",
            kind="text",
            label="App ID",
            i18n_key="channel.fields.app_id",
            required=True,
            placeholder="cli_xxxxxxxxxxxxxxxx",
        ),
        FormField(
            path="credential.app_secret",
            kind="password",
            label="App Secret",
            i18n_key="channel.fields.app_secret",
            required=True,
            secret=True,
        ),
        FormField(
            path="domain",
            kind="select",
            label="Domain",
            i18n_key="channel.fields.domain",
            required=True,
            default="feishu",
            # Feishu (mainland) and Lark (international) are separate API hosts,
            # so this picks an endpoint rather than a display preference.
            options=[
                FieldOption(value="feishu", label="Feishu (mainland)"),
                FieldOption(value="lark", label="Lark (international)"),
            ],
        ),
        FormField(
            path="allowed_open_ids",
            kind="string_list",
            label="Allowed open IDs",
            i18n_key="channel.fields.allowed_open_ids",
            max_items=1000,
        ),
    ]
)


PROVIDER_SPEC = ProviderSpec(
    name="feishu",
    display_name="Feishu / Lark",
    form=_FORM,
    capabilities=ProviderCapabilities(
        private_chat=True,
        group_chat=False,
        text=True,
        files=False,
        images=False,
        streaming_cards=False,
    ),
    config_model=FeishuConfigInput,
    config_patch_model=FeishuConfigPatch,
    credential_paths=frozenset({"credential.app_id", "credential.app_secret"}),
    # ``app_id`` is a non-secret identifier and legitimately stays in the public
    # config; only the secret half is encrypted.
    secret_paths=frozenset({"credential.app_secret"}),
    account_identity_path="credential.app_id",
    description="Answer direct messages from a Feishu or Lark bot over a long-lived connection.",
    description_i18n_key="channel.providers.feishu.description",
)
