"""Pure WeCom management specification (CHN-P15)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from api.channel_providers.spec import FormField, ProviderCapabilities, ProviderForm, ProviderSpec


class WeComCredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    corp_id: str | None = Field(default=None, min_length=1, max_length=128)
    agent_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[1-9][0-9]*$")
    corp_secret: SecretStr | None = Field(default=None, json_schema_extra={"writeOnly": True})
    callback_token: SecretStr | None = Field(default=None, json_schema_extra={"writeOnly": True})
    aes_key: SecretStr | None = Field(default=None, json_schema_extra={"writeOnly": True}, min_length=43, max_length=43)


class WeComConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: WeComCredentialInput = Field(default_factory=WeComCredentialInput)
    allowed_user_ids: list[str] = Field(default_factory=list, max_length=1000)


class WeComConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: WeComCredentialInput | None = None
    allowed_user_ids: list[str] | None = Field(default=None, max_length=1000)


PROVIDER_SPEC = ProviderSpec(
    name="wecom",
    display_name="WeCom",
    description="Receive encrypted callbacks at /api/v1/chat-channels/callbacks/wecom/{binding_id} on the API HTTPS origin.",
    form=ProviderForm(
        fields=[
            FormField(path="credential.corp_id", kind="text", label="Corp Id", required=True, secret=False),
            FormField(path="credential.agent_id", kind="text", label="Agent Id", required=True, secret=False),
            FormField(path="credential.corp_secret", kind="password", label="Corp Secret", required=True, secret=True),
            FormField(path="credential.callback_token", kind="password", label="Callback Token", required=True, secret=True),
            FormField(path="credential.aes_key", kind="password", label="Aes Key", required=True, secret=True),
            FormField(path="allowed_user_ids", kind="string_list", label="Allowed user IDs", max_items=1000),
        ]
    ),
    capabilities=ProviderCapabilities(private_chat=True, group_chat=False, text=True, files=False, images=False, streaming_cards=False, threaded_reply=False),
    config_model=WeComConfigInput,
    config_patch_model=WeComConfigPatch,
    credential_paths=frozenset({"credential.corp_id", "credential.agent_id", "credential.corp_secret", "credential.callback_token", "credential.aes_key"}),
    secret_paths=frozenset({"credential.corp_secret", "credential.callback_token", "credential.aes_key"}),
    account_identity_path="credential.corp_id",
    account_identity_components=("credential.corp_id", "credential.agent_id"),
)
