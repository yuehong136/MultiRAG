"""DingTalk provider specification.

The second provider, and the one the whole spec layer exists to make cheap.
Everything a client needs to render, validate and store a DingTalk channel is
declared here; nothing in ``api/channel_control`` or ``web/`` knows this file
exists. That is the acceptance criterion for the provider work (CHN-P10): a
frontend build made before DingTalk existed renders its form unchanged.

Deliberately no transport. Receiving DingTalk messages needs a stream client
that this project does not depend on yet, and the registry keeps the spec half
and the transport half as separate modules precisely so one can land without
the other. See PROGRESS.md CHN-P10 for what registration is waiting on.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from api.channel_providers.spec import (
    FormField,
    ProviderCapabilities,
    ProviderForm,
    ProviderSpec,
)


class DingTalkCredentialInput(BaseModel):
    """Write-only DingTalk app credentials accepted from the management UI."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    # DingTalk renamed these from AppKey/AppSecret; the current console calls
    # them Client ID and Client Secret, so that is what an admin is copying.
    client_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_secret: SecretStr | None = Field(
        default=None,
        json_schema_extra={"writeOnly": True},
    )


class DingTalkConfigInput(BaseModel):
    """Public DingTalk connection settings plus an optional write-only secret."""

    model_config = ConfigDict(extra="forbid")

    credential: DingTalkCredentialInput = Field(default_factory=DingTalkCredentialInput)
    # Which robot the app answers as. Unlike Feishu's domain this is not an
    # endpoint switch, so it has no options list and no default.
    robot_code: str | None = Field(default=None, min_length=1, max_length=128)
    allowed_user_ids: list[str] = Field(default_factory=list, max_length=1000)


class DingTalkCredentialPatch(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    client_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_secret: SecretStr | None = Field(
        default=None,
        json_schema_extra={"writeOnly": True},
    )


class DingTalkConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: DingTalkCredentialPatch | None = None
    robot_code: str | None = Field(default=None, min_length=1, max_length=128)
    allowed_user_ids: list[str] | None = Field(default=None, max_length=1000)


# Every field below uses a `kind` the client already renders. That is the whole
# test of whether the FieldSpec contract generalised: a provider whose shape
# differs from Feishu's -- different credential leaf names, no endpoint select,
# an extra free-text field -- still needs no new widget.
_FORM = ProviderForm(
    fields=[
        FormField(
            path="credential.client_id",
            kind="text",
            label="Client ID",
            i18n_key="channel.fields.client_id",
            required=True,
            placeholder="dingxxxxxxxxxxxxxxxx",
            help_text="Called AppKey in older DingTalk consoles.",
        ),
        FormField(
            path="credential.client_secret",
            kind="password",
            label="Client Secret",
            i18n_key="channel.fields.client_secret",
            required=True,
            secret=True,
        ),
        FormField(
            path="robot_code",
            kind="text",
            label="Robot code",
            i18n_key="channel.fields.robot_code",
            required=True,
        ),
        FormField(
            path="allowed_user_ids",
            kind="string_list",
            label="Allowed user IDs",
            i18n_key="channel.fields.allowed_user_ids",
            max_items=1000,
        ),
    ]
)

PROVIDER_SPEC = ProviderSpec(
    name="dingtalk",
    display_name="DingTalk",
    form=_FORM,
    capabilities=ProviderCapabilities(
        private_chat=True,
        # DingTalk robots do receive group messages, but this stays False until
        # a transport exists to prove it: the worker ORs this with the admin
        # policy and the narrower one wins, so declaring a capability we cannot
        # serve would let an admin open group traffic to nothing.
        group_chat=False,
        text=True,
        files=False,
        images=False,
        streaming_cards=False,
    ),
    config_model=DingTalkConfigInput,
    config_patch_model=DingTalkConfigPatch,
    credential_paths=frozenset({"credential.client_id", "credential.client_secret"}),
    # `client_id` is a non-secret identifier and stays in the public config, the
    # same split Feishu uses for `app_id`.
    secret_paths=frozenset({"credential.client_secret"}),
    account_identity_path="credential.client_id",
)
