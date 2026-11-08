"""Pure Telegram management specification (CHN-P15)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from api.channel_providers.spec import FormField, ProviderCapabilities, ProviderForm, ProviderSpec


class TelegramCredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    bot_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[1-9][0-9]*$")
    bot_token: SecretStr | None = Field(default=None, json_schema_extra={"writeOnly": True})


class TelegramConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: TelegramCredentialInput = Field(default_factory=TelegramCredentialInput)
    allowed_user_ids: list[str] = Field(default_factory=list, max_length=1000)


class TelegramConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: TelegramCredentialInput | None = None
    allowed_user_ids: list[str] | None = Field(default=None, max_length=1000)


PROVIDER_SPEC = ProviderSpec(
    name="telegram",
    display_name="Telegram",
    description="Receive text through Bot API long polling. Remove any existing webhook before enabling.",
    form=ProviderForm(
        fields=[
            FormField(path="credential.bot_id", kind="text", label="Bot Id", required=True, secret=False),
            FormField(path="credential.bot_token", kind="password", label="Bot Token", required=True, secret=True),
            FormField(path="allowed_user_ids", kind="string_list", label="Allowed user IDs", max_items=1000),
        ]
    ),
    capabilities=ProviderCapabilities(private_chat=True, group_chat=True, text=True, files=False, images=False, streaming_cards=False, threaded_reply=True),
    config_model=TelegramConfigInput,
    config_patch_model=TelegramConfigPatch,
    credential_paths=frozenset({"credential.bot_id", "credential.bot_token"}),
    secret_paths=frozenset({"credential.bot_token"}),
    account_identity_path="credential.bot_id",
)
