"""Pure LINE management specification (CHN-P15)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from api.channel_providers.spec import FormField, ProviderCapabilities, ProviderForm, ProviderSpec


class LineCredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    channel_id: str | None = Field(default=None, min_length=1, max_length=128)
    channel_secret: SecretStr | None = Field(default=None, json_schema_extra={"writeOnly": True})
    access_token: SecretStr | None = Field(default=None, json_schema_extra={"writeOnly": True})


class LineConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: LineCredentialInput = Field(default_factory=LineCredentialInput)
    allowed_user_ids: list[str] = Field(default_factory=list, max_length=1000)


class LineConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: LineCredentialInput | None = None
    allowed_user_ids: list[str] | None = Field(default=None, max_length=1000)


PROVIDER_SPEC = ProviderSpec(
    name="line",
    display_name="LINE",
    description="Receive signed callbacks at /api/v1/chat-channels/callbacks/line/{binding_id} on the API HTTPS origin.",
    form=ProviderForm(
        fields=[
            FormField(path="credential.channel_id", kind="text", label="Channel Id", required=True, secret=False),
            FormField(path="credential.channel_secret", kind="password", label="Channel Secret", required=True, secret=True),
            FormField(path="credential.access_token", kind="password", label="Access Token", required=True, secret=True),
            FormField(path="allowed_user_ids", kind="string_list", label="Allowed user IDs", max_items=1000),
        ]
    ),
    capabilities=ProviderCapabilities(private_chat=True, group_chat=True, text=True, files=False, images=False, streaming_cards=False, threaded_reply=False),
    config_model=LineConfigInput,
    config_patch_model=LineConfigPatch,
    credential_paths=frozenset({"credential.channel_id", "credential.channel_secret", "credential.access_token"}),
    secret_paths=frozenset({"credential.channel_secret", "credential.access_token"}),
    account_identity_path="credential.channel_id",
)
