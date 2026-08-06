"""Channel control-plane contracts, isolation, and credential safety."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from api.channel_control.dependencies import get_channel_control_service
from api.channel_control.repository import ModelT
from api.channel_control.schemas import (
    ChannelBindingUpsertRequest,
    ChannelCreateRequest,
    ChannelUpdateRequest,
    FeishuConfigInput,
    FeishuConfigPatch,
)
from api.channel_control.secret_store import AESGCMChannelSecretStore, EncryptedSecret, SecretStoreUnavailable, UnavailableSecretStore
from api.channel_control.service import (
    _REVISION_STALE_ERROR_CODE,
    ChannelAccessDenied,
    ChannelControlService,
    ChannelCredentialUnavailable,
    ChannelTargetNotAccessible,
    ChannelVerificationInconclusive,
    ChannelVerificationNotSupported,
    ChannelVerificationRejected,
    ChannelVerificationThrottled,
    InvalidChannelConfiguration,
    _contains_sensitive_key,
    _sanitize_public_config,
)
from api.channel_control.verification_throttle import VerificationThrottle
from api.channel_execution.errors import TargetRevisionUnavailableError
from api.channel_providers import provider_names
from api.channels.verification import ChannelCredentialRejected, ChannelVerificationUnavailable
from api.db.db_models import ChannelBinding, ChannelRuntimeStatus, ChannelSecret, ChatChannel
from common.app_config import get_app_config
from common.channel_secret_crypto import ChannelSecretCipher
from common.constants import RetCode


class FakeRepository:
    def __init__(self) -> None:
        self.channels: dict[str, ChatChannel] = {}
        self.secrets: dict[str, ChannelSecret] = {}
        self.bindings: dict[str, ChannelBinding] = {}
        self.runtimes: dict[str, ChannelRuntimeStatus] = {}
        self.dialogs: set[tuple[str, str]] = set()
        self.latest_canvas_revisions: set[tuple[str, str, str]] = set()
        # canvas_id -> (owning tenant, permission). Registering a revision also
        # implies the canvas exists, so same-tenant tests need not do both.
        self.canvases: dict[str, tuple[str, str]] = {}
        # (user_id, tenant_id) -> role, for cross-tenant authorization tests.
        self.tenant_roles: dict[tuple[str, str], str] = {}
        self.commits = 0
        self.rollbacks = 0

    async def list_channels(self, tenant_id: str) -> tuple[list[ChatChannel], int]:
        items = [channel for channel in self.channels.values() if channel.tenant_id == tenant_id]
        return items, len(items)

    async def get_channel(self, tenant_id: str, channel_id: str, *, for_update: bool = False) -> ChatChannel | None:
        del for_update
        channel = self.channels.get(channel_id)
        return channel if channel is not None and channel.tenant_id == tenant_id else None

    async def get_secret(self, channel_id: str, *, for_update: bool = False) -> ChannelSecret | None:
        del for_update
        return self.secrets.get(channel_id)

    async def get_binding(self, channel_id: str, *, for_update: bool = False) -> ChannelBinding | None:
        del for_update
        return self.bindings.get(channel_id)

    async def get_runtime(self, binding_id: str, *, for_update: bool = False) -> ChannelRuntimeStatus | None:
        del for_update
        return self.runtimes.get(binding_id)

    async def list_enabled_channels(self, tenant_id: str, provider: str) -> list[ChatChannel]:
        return [channel for channel in self.channels.values() if channel.tenant_id == tenant_id and channel.channel == provider and channel.status == 1]

    async def get_runtime_binding(
        self,
        binding_id: str,
        *,
        for_update: bool = False,
    ) -> tuple[ChatChannel, ChannelBinding, ChannelSecret | None] | None:
        del for_update
        binding = next((item for item in self.bindings.values() if item.id == binding_id), None)
        if binding is None:
            return None
        channel = self.channels[binding.channel_id]
        return channel, binding, self.secrets.get(channel.id)

    async def list_runtime_bindings(
        self,
    ) -> list[tuple[ChatChannel, ChannelBinding, ChannelSecret | None]]:
        bundles: list[tuple[ChatChannel, ChannelBinding, ChannelSecret | None]] = []
        for binding in self.bindings.values():
            channel = self.channels[binding.channel_id]
            if channel.status == 1 and binding.enabled:
                bundles.append((channel, binding, self.secrets.get(channel.id)))
        return bundles

    async def resolve_dialog_owner(self, dialog_id: str) -> str | None:
        for owner, known_dialog_id in self.dialogs:
            if known_dialog_id == dialog_id:
                return owner
        return None

    async def resolve_canvas_owner(self, canvas_id: str) -> tuple[str, str] | None:
        if canvas_id in self.canvases:
            return self.canvases[canvas_id]
        for owner, known_canvas_id, _revision in self.latest_canvas_revisions:
            if known_canvas_id == canvas_id:
                return owner, "me"
        return None

    async def user_can_update_tenant_resources(self, user_id: str, tenant_id: str) -> bool:
        if user_id == tenant_id:
            return True
        return self.tenant_roles.get((user_id, tenant_id)) in {"owner", "admin"}

    async def canvas_revision_is_latest_published(self, canvas_id: str, revision_id: str) -> bool:
        return any(known_canvas_id == canvas_id and known_revision == revision_id for _owner, known_canvas_id, known_revision in self.latest_canvas_revisions)

    def add(self, model: ModelT) -> None:
        if isinstance(model, ChatChannel):
            self.channels[model.id] = model
        elif isinstance(model, ChannelSecret):
            self.secrets[model.channel_id] = model
        elif isinstance(model, ChannelBinding):
            self.bindings[model.channel_id] = model
        elif isinstance(model, ChannelRuntimeStatus):
            self.runtimes[model.binding_id] = model
        else:  # pragma: no cover - protects the fake from silent drift
            raise AssertionError(type(model))

    async def delete(self, model: ModelT) -> None:
        if isinstance(model, ChatChannel):
            self.channels.pop(model.id, None)
            self.secrets.pop(model.id, None)
            binding = self.bindings.pop(model.id, None)
            if binding is not None:
                self.runtimes.pop(binding.id, None)
        elif isinstance(model, ChannelBinding):
            self.bindings.pop(model.channel_id, None)
            self.runtimes.pop(model.id, None)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def test_feishu_config_accepts_upstream_nested_domain_without_changing_canonical_shape() -> None:
    created = FeishuConfigInput.model_validate(
        {
            "credential": {
                "app_id": "cli_1",
                "app_secret": "secret",
                "domain": "lark",
            }
        }
    )
    patched = FeishuConfigPatch.model_validate(
        {
            "credential": {
                "domain": "lark",
            }
        }
    )

    assert created.domain == "lark"
    assert created.credential.model_dump(exclude_none=True) == {
        "app_id": "cli_1",
        "app_secret": created.credential.app_secret,
    }
    assert patched.domain == "lark"


def test_feishu_root_domain_wins_over_nested_compatibility_value() -> None:
    config = FeishuConfigInput.model_validate(
        {
            "credential": {"app_id": "cli_1", "domain": "lark"},
            "domain": "feishu",
        }
    )

    assert config.domain == "feishu"


class FakeSecretStore:
    def __init__(self) -> None:
        self.plaintexts: list[dict[str, str]] = []

    async def encrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        plaintext: Mapping[str, str],
        version: int,
    ) -> EncryptedSecret:
        del tenant_id, channel_id
        self.plaintexts.append(dict(plaintext))
        return EncryptedSecret(ciphertext=f"ciphertext-v{version}", key_id="unit-key", version=version)

    async def decrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        encrypted: EncryptedSecret,
    ) -> Mapping[str, str]:
        del tenant_id, channel_id
        # Round-trip for real. Returning {} unconditionally meant every caller
        # of resolve_runtime_binding saw "credential incomplete", so nothing
        # ever reached the code that reassembles a credential for a runner.
        for stored in reversed(self.plaintexts):
            if stored:
                return dict(stored)
        return {}


def _create_request(*, chat_id: str | None = None, status: int = 0, app_id: str = "cli_unit") -> ChannelCreateRequest:
    return ChannelCreateRequest.model_validate(
        {
            "name": "Leadership demo",
            "channel": "feishu",
            "config": {
                "credential": {
                    "app_id": app_id,
                    "app_secret": "never-return-this-secret",
                }
            },
            "chat_id": chat_id,
            "status": status,
        }
    )


async def test_secret_is_encrypted_and_never_returned() -> None:
    repository = FakeRepository()
    secret_store = FakeSecretStore()
    service = ChannelControlService(repository, secret_store)

    response = await service.create_channel("tenant-a", _create_request())

    assert secret_store.plaintexts == [{"app_secret": "never-return-this-secret"}]
    assert response["secret"] == {"configured": True, "version": 1}
    assert "never-return-this-secret" not in repr(response)
    assert "app_secret" not in repr(response["config"])
    stored = repository.secrets[response["id"]]
    assert stored.ciphertext == "ciphertext-v1"
    assert "never-return-this-secret" not in stored.ciphertext


async def test_unavailable_secret_store_fails_closed() -> None:
    service = ChannelControlService(FakeRepository(), UnavailableSecretStore())

    with pytest.raises(ChannelCredentialUnavailable):
        await service.create_channel("tenant-a", _create_request())


async def test_tenant_scope_hides_another_tenants_channel() -> None:
    repository = FakeRepository()
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request())

    with pytest.raises(ChannelAccessDenied):
        await service.get_channel("tenant-b", created["id"])


async def test_legacy_chat_id_maps_to_multirag_dialog() -> None:
    repository = FakeRepository()
    repository.dialogs.add(("tenant-a", "dialog-1"))
    service = ChannelControlService(repository, FakeSecretStore())

    response = await service.create_channel("tenant-a", _create_request(chat_id="dialog-1"))

    assert response["chat_id"] == "dialog-1"
    assert response["binding"]["target_type"] == "multirag.dialog"
    assert response["binding"]["target_id"] == "dialog-1"


async def test_canvas_binding_requires_latest_owned_published_revision() -> None:
    repository = FakeRepository()
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request())
    # The canvas exists and is ours; only its bound revision is out of date.
    # "Agent unavailable" and "revision stale" are now two distinct answers.
    repository.canvases["agent-1"] = ("tenant-a", "me")
    request = ChannelBindingUpsertRequest(
        target_type="multirag.canvas_agent",
        target_id="agent-1",
        target_revision_id="revision-1",
    )

    with pytest.raises(InvalidChannelConfiguration, match="not the latest published"):
        await service.upsert_binding("tenant-a", created["id"], request)

    repository.latest_canvas_revisions.add(("tenant-a", "agent-1", "revision-1"))
    response = await service.upsert_binding("tenant-a", created["id"], request)
    assert response["binding"]["target_type"] == "multirag.canvas_agent"
    assert response["binding"]["target_revision_id"] == "revision-1"


async def test_team_shared_agent_binds_for_a_tenant_updater() -> None:
    """The live bug: the dropdown lists team targets, the backend refused them.

    The frontend lists bindable targets in team scope, so a shared Agent shows
    up, gets picked, and used to be rejected -- with the reason swallowed by a
    bare catch in the UI.
    """

    repository = FakeRepository()
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request())
    repository.canvases["agent-shared"] = ("tenant-owner", "team")
    repository.latest_canvas_revisions.add(("tenant-owner", "agent-shared", "revision-1"))
    repository.tenant_roles[("tenant-a", "tenant-owner")] = "admin"

    response = await service.upsert_binding(
        "tenant-a",
        created["id"],
        ChannelBindingUpsertRequest(
            target_type="multirag.canvas_agent",
            target_id="agent-shared",
            target_revision_id="revision-1",
        ),
    )

    assert response["binding"]["target_id"] == "agent-shared"


async def test_team_shared_agent_is_refused_to_a_plain_member() -> None:
    """Widening the lookup without a role check would be the worse bug.

    Binding re-publishes someone's Agent to an entire external workspace, so
    reading it is not enough.
    """

    repository = FakeRepository()
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request())
    repository.canvases["agent-shared"] = ("tenant-owner", "team")
    repository.latest_canvas_revisions.add(("tenant-owner", "agent-shared", "revision-1"))
    repository.tenant_roles[("tenant-a", "tenant-owner")] = "normal"

    with pytest.raises(ChannelTargetNotAccessible, match="do not have permission"):
        await service.upsert_binding(
            "tenant-a",
            created["id"],
            ChannelBindingUpsertRequest(
                target_type="multirag.canvas_agent",
                target_id="agent-shared",
                target_revision_id="revision-1",
            ),
        )


async def test_private_agent_of_another_tenant_is_refused_even_to_an_admin() -> None:
    """``permission='me'`` is the owner's own decision; a role cannot override it."""

    repository = FakeRepository()
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request())
    repository.canvases["agent-private"] = ("tenant-owner", "me")
    repository.latest_canvas_revisions.add(("tenant-owner", "agent-private", "revision-1"))
    repository.tenant_roles[("tenant-a", "tenant-owner")] = "admin"

    with pytest.raises(ChannelTargetNotAccessible):
        await service.upsert_binding(
            "tenant-a",
            created["id"],
            ChannelBindingUpsertRequest(
                target_type="multirag.canvas_agent",
                target_id="agent-private",
                target_revision_id="revision-1",
            ),
        )


async def test_dialog_of_a_joined_tenant_follows_the_same_role_rule() -> None:
    """Dialogs have no per-object share flag, so membership + role is the whole test."""

    repository = FakeRepository()
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request())
    repository.dialogs.add(("tenant-owner", "dialog-shared"))
    request = ChannelBindingUpsertRequest(
        target_type="multirag.dialog",
        target_id="dialog-shared",
    )

    with pytest.raises(ChannelTargetNotAccessible):
        await service.upsert_binding("tenant-a", created["id"], request)

    repository.tenant_roles[("tenant-a", "tenant-owner")] = "owner"
    response = await service.upsert_binding("tenant-a", created["id"], request)

    assert response["binding"]["target_id"] == "dialog-shared"


async def test_a_missing_target_is_reported_separately_from_an_unauthorized_one() -> None:
    repository = FakeRepository()
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request())

    with pytest.raises(InvalidChannelConfiguration, match="dialog is unavailable"):
        await service.upsert_binding(
            "tenant-a",
            created["id"],
            ChannelBindingUpsertRequest(target_type="multirag.dialog", target_id="ghost"),
        )

    with pytest.raises(InvalidChannelConfiguration, match="agent is unavailable"):
        await service.upsert_binding(
            "tenant-a",
            created["id"],
            ChannelBindingUpsertRequest(
                target_type="multirag.canvas_agent",
                target_id="ghost",
                target_revision_id="revision-1",
            ),
        )


async def test_read_paths_flag_a_stale_canvas_revision_without_mutating_state() -> None:
    repository = FakeRepository()
    repository.latest_canvas_revisions.add(("tenant-a", "agent-1", "revision-1"))
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request())
    channel_id = created["id"]
    upserted = await service.upsert_binding(
        "tenant-a",
        channel_id,
        ChannelBindingUpsertRequest(
            target_type="multirag.canvas_agent",
            target_id="agent-1",
            target_revision_id="revision-1",
        ),
    )
    # Mutation responses resolve no staleness, exactly like ``runtime``.
    assert upserted["binding"]["revision_stale"] is None
    assert upserted["runtime"] is None

    fresh = await service.get_channel("tenant-a", channel_id)
    assert fresh["binding"]["revision_stale"] is False

    # Publishing a newer Canvas release strands the bound revision.
    repository.latest_canvas_revisions.discard(("tenant-a", "agent-1", "revision-1"))
    repository.latest_canvas_revisions.add(("tenant-a", "agent-1", "revision-2"))

    stale = await service.get_channel("tenant-a", channel_id)
    assert stale["binding"]["revision_stale"] is True
    # The hint is read-only: it must not rebind or advance a generation.
    assert stale["binding"]["target_revision_id"] == "revision-1"
    assert stale["binding"]["generation"] == fresh["binding"]["generation"]
    assert stale["generation"] == fresh["generation"]
    # This binding is disabled, so nothing is expected to be running and the
    # runtime block stays untouched. An *enabled* stale binding does change it --
    # see ``test_a_stale_revision_is_a_fault_even_while_the_runner_looks_healthy``.
    assert stale["binding"]["enabled"] is False
    assert stale["runtime"] == fresh["runtime"]

    listed = await service.list_channels("tenant-a")
    assert [item["binding"]["revision_stale"] for item in listed["items"]] == [True]


async def _enabled_canvas_channel() -> tuple[ChannelControlService, FakeRepository, str, str, int]:
    repository = FakeRepository()
    repository.canvases["agent-1"] = ("tenant-a", "me")
    repository.latest_canvas_revisions.add(("tenant-a", "agent-1", "revision-1"))
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request())
    channel_id = created["id"]
    await service.upsert_binding(
        "tenant-a",
        channel_id,
        ChannelBindingUpsertRequest(
            target_type="multirag.canvas_agent",
            target_id="agent-1",
            target_revision_id="revision-1",
        ),
    )
    enabled = await service.set_enabled("tenant-a", channel_id, enabled=True)
    return service, repository, channel_id, enabled["binding"]["id"], enabled["binding"]["generation"]


async def test_a_stale_revision_is_a_fault_even_while_the_runner_looks_healthy() -> None:
    service, repository, channel_id, binding_id, generation = await _enabled_canvas_channel()
    now = datetime.now(UTC)
    await service.report_runtime(
        binding_id=binding_id,
        observed_generation=generation,
        state="connected",
        runner_id="runner-1",
        heartbeat_at=now,
        connected_at=now,
    )

    healthy = await service.get_runtime("tenant-a", channel_id)
    assert healthy["state"] == "connected"
    assert healthy["last_error_code"] is None

    # Publishing a newer release strands the bound revision. Nothing about the
    # runner changes: it stays alive, on-generation and heartbeating, which is
    # exactly what used to hide the fault -- every message now fails in the
    # executor while this panel reported a healthy channel.
    repository.latest_canvas_revisions.discard(("tenant-a", "agent-1", "revision-1"))
    repository.latest_canvas_revisions.add(("tenant-a", "agent-1", "revision-2"))

    faulted = await service.get_runtime("tenant-a", channel_id)
    assert faulted["state"] == "error"
    assert faulted["last_error_code"] == _REVISION_STALE_ERROR_CODE
    # The runner really is alive and the operator needs to know which one.
    assert faulted["runner_id"] == "runner-1"
    assert faulted["observed_generation"] == generation

    # The dedicated route and the embedded block must not disagree -- the whole
    # point of this item is that they used to.
    detail = await service.get_channel("tenant-a", channel_id)
    assert detail["runtime"] == faulted
    assert detail["binding"]["revision_stale"] is True


async def test_a_disabled_stale_binding_is_stopped_rather_than_faulted() -> None:
    service, repository, channel_id, _binding_id, _generation = await _enabled_canvas_channel()
    repository.latest_canvas_revisions.discard(("tenant-a", "agent-1", "revision-1"))
    repository.latest_canvas_revisions.add(("tenant-a", "agent-1", "revision-2"))
    await service.set_enabled("tenant-a", channel_id, enabled=False)

    runtime = await service.get_runtime("tenant-a", channel_id)

    # Deliberately off is not broken. Reporting ``error`` here would put a red
    # badge on every channel an operator ever paused, and the staleness is still
    # legible on ``binding.revision_stale``.
    assert runtime["state"] == "stopped"
    assert runtime["last_error_code"] is None
    assert (await service.get_channel("tenant-a", channel_id))["binding"]["revision_stale"] is True


def test_stale_revision_error_code_matches_the_executor() -> None:
    # The control plane duplicates this string instead of importing it, because
    # ``api.channel_execution`` imports ``api.channel_control``. One fact, one
    # code: an operator greps a single string across the panel and the logs.
    assert _REVISION_STALE_ERROR_CODE == TargetRevisionUnavailableError.code


async def test_dialog_binding_reports_no_revision_staleness() -> None:
    repository = FakeRepository()
    repository.dialogs.add(("tenant-a", "dialog-1"))
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request(chat_id="dialog-1"))

    detail = await service.get_channel("tenant-a", created["id"])

    assert detail["binding"]["target_type"] == "multirag.dialog"
    assert detail["binding"]["revision_stale"] is None


def test_target_type_rejects_external_namespace() -> None:
    with pytest.raises(ValidationError):
        ChannelBindingUpsertRequest.model_validate(
            {
                "target_type": "external.dialog",
                "target_id": "dialog-1",
            }
        )


async def test_enable_disable_is_idempotent_and_advances_generation() -> None:
    repository = FakeRepository()
    repository.dialogs.add(("tenant-a", "dialog-1"))
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request(chat_id="dialog-1"))
    channel_id = created["id"]

    enabled = await service.set_enabled("tenant-a", channel_id, enabled=True)
    assert enabled["status"] == 1
    assert enabled["generation"] == 2
    assert enabled["binding"]["generation"] == 2

    enabled_again = await service.set_enabled("tenant-a", channel_id, enabled=True)
    assert enabled_again["generation"] == 2
    assert enabled_again["binding"]["generation"] == 2

    disabled = await service.set_enabled("tenant-a", channel_id, enabled=False)
    assert disabled["status"] == 0
    assert disabled["generation"] == 3
    assert disabled["binding"]["generation"] == 3


async def test_the_resolver_hands_a_runner_a_whole_credential_and_its_own_policy() -> None:
    """Emit halves of CHN-P4 -> CHN-P8 and CHN-O2 -> CHN-O3.

    Both used to stop at the database. ``policy.private_chat_only`` was
    collected, validated and stored, and then never reached the process that
    enforces it; the generic credential map existed but was withheld from the
    wire while older runners were still being taught to accept it.
    """

    repository = FakeRepository()
    repository.dialogs.add(("tenant-a", "dialog-1"))
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request(chat_id="dialog-1"))
    await service.upsert_binding(
        "tenant-a",
        created["id"],
        ChannelBindingUpsertRequest(
            target_type="multirag.dialog",
            target_id="dialog-1",
            policy={"private_chat_only": False},
        ),
    )
    enabled = await service.set_enabled("tenant-a", created["id"], enabled=True)

    resolved = await service.resolve_runtime_binding(enabled["binding"]["id"])

    assert resolved.policy == {"private_chat_only": False}
    # The credential is whole: the encrypted half plus the non-secret half that
    # legitimately lives in the public config, reassembled under the leaf names
    # the provider spec declares. That reassembly is what lets a second
    # provider work without the runtime route naming any of its fields.
    assert resolved.credentials["app_secret"] == "never-return-this-secret"
    assert resolved.credentials["app_id"] == "cli_unit"


async def test_a_runner_policy_is_the_stored_one_and_still_carries_no_credential() -> None:
    repository = FakeRepository()
    repository.dialogs.add(("tenant-a", "dialog-1"))
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request(chat_id="dialog-1"))

    # Handing the policy to a runner verbatim is only safe because this check
    # exists; it is what makes the free-form column releasable at all.
    with pytest.raises(InvalidChannelConfiguration, match="credentials"):
        await service.upsert_binding(
            "tenant-a",
            created["id"],
            ChannelBindingUpsertRequest(
                target_type="multirag.dialog",
                target_id="dialog-1",
                policy={"private_chat_only": True, "app_secret": "leaked"},
            ),
        )

    # Unknown but harmless keys ride along untouched, so a future toggle needs
    # no control-plane change to reach the runner.
    await service.upsert_binding(
        "tenant-a",
        created["id"],
        ChannelBindingUpsertRequest(
            target_type="multirag.dialog",
            target_id="dialog-1",
            policy={"private_chat_only": True, "locale": "zh-CN"},
        ),
    )
    enabled = await service.set_enabled("tenant-a", created["id"], enabled=True)
    resolved = await service.resolve_runtime_binding(enabled["binding"]["id"])
    assert resolved.policy == {"private_chat_only": True, "locale": "zh-CN"}


def test_an_unregistered_provider_is_refused_by_the_registry_not_by_a_literal() -> None:
    """CHN-P9: the request schema reads the registry instead of naming feishu.

    The old `Literal["feishu"]` meant adding a provider required editing the
    control plane's request model -- one of the places that gets forgotten,
    and the reason "multi-provider" was true of the registry and false of the
    API.
    """

    with pytest.raises(ValidationError, match="unknown channel provider"):
        ChannelCreateRequest.model_validate({"name": "Demo", "channel": "wecom", "config": {}})

    # Every registered name is accepted without listing any of them here.
    for name in provider_names():
        parsed = ChannelCreateRequest.model_validate(
            {
                "name": "Demo",
                "channel": name,
                "config": {},
            }
        )
        assert parsed.channel == name


async def test_a_rejected_config_names_the_field_and_never_echoes_the_value() -> None:
    """The error body and the logs behind it must not carry a rejected secret.

    ``ValidationError.errors()`` carries an ``input`` key, so the obvious
    implementation -- forwarding pydantic's message verbatim -- would put a
    submitted ``app_secret`` into an API response.
    """

    repository = FakeRepository()
    service = ChannelControlService(repository, FakeSecretStore())

    with pytest.raises(InvalidChannelConfiguration) as caught:
        await service.create_channel(
            "tenant-a",
            ChannelCreateRequest.model_validate(
                {
                    "name": "Demo",
                    "channel": "feishu",
                    "config": {
                        "credential": {"app_id": "cli_unit", "app_secret": "never-return-this-secret"},
                        "domain": "not-a-real-domain",
                    },
                }
            ),
        )

    message = str(caught.value)
    assert "domain" in message
    assert "never-return-this-secret" not in message
    assert "not-a-real-domain" not in message


async def test_a_patch_is_validated_against_the_stored_channel_provider() -> None:
    """A PATCH body carries no provider name; only the row knows which it is."""

    repository = FakeRepository()
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request())

    with pytest.raises(InvalidChannelConfiguration, match="feishu"):
        await service.update_channel(
            "tenant-a",
            created["id"],
            ChannelUpdateRequest.model_validate({"config": {"domain": "not-a-real-domain"}}),
        )

    # A well-formed patch still merges, so the open type did not weaken anything.
    updated = await service.update_channel(
        "tenant-a",
        created["id"],
        ChannelUpdateRequest.model_validate({"config": {"domain": "lark"}}),
    )
    assert updated["config"]["domain"] == "lark"


async def test_runtime_bundle_and_heartbeat_are_internal_and_generation_fenced() -> None:
    repository = FakeRepository()
    repository.dialogs.add(("tenant-a", "dialog-1"))
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request(chat_id="dialog-1"))
    enabled = await service.set_enabled("tenant-a", created["id"], enabled=True)
    binding_id = enabled["binding"]["id"]

    spec = await service.load_runtime_binding(binding_id)
    assert spec.tenant_id == "tenant-a"
    assert spec.target_type == "multirag.dialog"
    assert spec.encrypted_secret.ciphertext == "ciphertext-v1"

    heartbeat = datetime.now(UTC)
    await service.report_runtime(
        binding_id=binding_id,
        observed_generation=spec.generation,
        state="connected",
        runner_id="runner-1",
        heartbeat_at=heartbeat,
        connected_at=heartbeat,
    )
    runtime = await service.get_runtime("tenant-a", created["id"])
    assert runtime["state"] == "connected"
    assert runtime["runner_id"] == "runner-1"
    detail = await service.get_channel("tenant-a", created["id"])
    assert detail["runtime"]["state"] == "connected"
    assert detail["runtime"]["runner_id"] == "runner-1"

    with pytest.raises(InvalidChannelConfiguration, match="generation"):
        await service.report_runtime(
            binding_id=binding_id,
            observed_generation=spec.generation + 1,
            state="connected",
            runner_id="runner-1",
            heartbeat_at=heartbeat,
        )

    await service.set_enabled("tenant-a", created["id"], enabled=False)
    stopped = await service.get_runtime("tenant-a", created["id"])
    assert stopped["desired_generation"] == spec.generation + 1
    assert stopped["observed_generation"] == spec.generation
    assert stopped["state"] == "stopped"
    assert stopped["runner_id"] is None
    with pytest.raises(InvalidChannelConfiguration, match="generation"):
        await service.report_runtime(
            binding_id=binding_id,
            observed_generation=spec.generation,
            state="stopped",
            runner_id="runner-1",
            heartbeat_at=heartbeat,
        )


async def _enabled_dialog_channel() -> tuple[ChannelControlService, str, str, int]:
    repository = FakeRepository()
    repository.dialogs.add(("tenant-a", "dialog-1"))
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request(chat_id="dialog-1"))
    enabled = await service.set_enabled("tenant-a", created["id"], enabled=True)
    return service, created["id"], enabled["binding"]["id"], enabled["binding"]["generation"]


async def test_live_runtime_state_expires_once_the_heartbeat_stops() -> None:
    service, channel_id, binding_id, generation = await _enabled_dialog_channel()
    interval = get_app_config().channels.control.runtime_heartbeat_seconds

    fresh = datetime.now(UTC)
    await service.report_runtime(
        binding_id=binding_id,
        observed_generation=generation,
        state="connected",
        runner_id="runner-1",
        heartbeat_at=fresh,
        connected_at=fresh,
    )
    assert (await service.get_runtime("tenant-a", channel_id))["state"] == "connected"

    # A killed worker leaves its last row behind: the generation still matches, so
    # only heartbeat silence can disprove "connected".
    dead = datetime.now(UTC) - timedelta(seconds=interval * 10)
    await service.report_runtime(
        binding_id=binding_id,
        observed_generation=generation,
        state="connected",
        runner_id="runner-1",
        heartbeat_at=dead,
        connected_at=dead,
        last_error_code="WORKER_GONE",
    )

    stale = await service.get_runtime("tenant-a", channel_id)
    assert stale["state"] == "waiting"
    assert stale["runner_id"] is None
    assert stale["connected_at"] is None
    # The last heartbeat and error code are the only evidence of when it died.
    assert stale["heartbeat_at"] is not None
    assert stale["last_error_code"] == "WORKER_GONE"
    assert stale["observed_generation"] == generation


async def test_non_live_runtime_states_survive_heartbeat_silence() -> None:
    service, channel_id, binding_id, generation = await _enabled_dialog_channel()

    # ``error`` is terminal: no runner is expected to keep reporting, so silence
    # must not be mistaken for staleness and must not erase the diagnosis.
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    await service.report_runtime(
        binding_id=binding_id,
        observed_generation=generation,
        state="error",
        runner_id="runner-1",
        heartbeat_at=long_ago,
        last_error_code="CHANNEL_RUNTIME_CONFIG_INVALID",
    )

    reported = await service.get_runtime("tenant-a", channel_id)
    assert reported["state"] == "error"
    assert reported["runner_id"] == "runner-1"
    assert reported["last_error_code"] == "CHANNEL_RUNTIME_CONFIG_INVALID"


def test_provider_and_list_routes_use_stable_envelopes(client) -> None:
    class StubService:
        async def list_channels(self, tenant_id: str) -> dict[str, Any]:
            assert tenant_id == "user-unit"
            return {"items": [], "total": 0}

    client.app.dependency_overrides[get_channel_control_service] = StubService

    provider_body = client.get("/api/v1/chat-channels/providers").json()
    assert provider_body["retcode"] == int(RetCode.SUCCESS)
    # Derived, not pinned: this asserted `items[0]["provider"] == "feishu"`
    # until CHN-P10 registered a second provider and sorting put it first. The
    # envelope shape is what this test is for; which providers exist is
    # `test_channel_provider_spec.py`'s business.
    assert [item["provider"] for item in provider_body["data"]["items"]] == list(provider_names())
    assert set(provider_body["data"]["items"][0]) >= {
        "provider",
        "display_name",
        "capabilities",
        "config_schema",
    }

    list_body = client.get("/api/v1/chat-channels").json()
    assert list_body == {
        "retcode": int(RetCode.SUCCESS),
        "retmsg": "success",
        "data": {"items": [], "total": 0},
    }


def test_failure_envelope_carries_a_machine_readable_error_code(client) -> None:
    """Operationally distinct failures must not reach the UI as one blob.

    The service layer has always produced these codes; the route boundary used
    to drop them and return ``data=False``, leaving the admin with a single
    "operation failed" for four failures whose fixes have nothing in common.
    """

    class StubService:
        async def set_enabled(self, tenant_id: str, channel_id: str, *, enabled: bool) -> dict[str, Any]:
            del tenant_id, channel_id, enabled
            raise ChannelTargetNotAccessible()

        async def delete_channel(self, tenant_id: str, channel_id: str) -> bool:
            del tenant_id, channel_id
            raise ChannelAccessDenied()

        async def get_channel(self, tenant_id: str, channel_id: str) -> dict[str, Any]:
            del tenant_id, channel_id
            raise RuntimeError("boom: internal detail that must not escape")

    client.app.dependency_overrides[get_channel_control_service] = StubService

    refused = client.post("/api/v1/chat-channels/channel-1/enable").json()
    assert refused["retcode"] == int(RetCode.ARGUMENT_ERROR)
    assert refused["data"] == {"error_code": "CHANNEL_TARGET_NOT_ACCESSIBLE"}
    assert "permission" in refused["retmsg"]

    denied = client.delete("/api/v1/chat-channels/channel-1").json()
    assert denied["retcode"] == int(RetCode.AUTHENTICATION_ERROR)
    assert denied["data"] == {"error_code": "CHANNEL_NOT_ACCESSIBLE"}

    # The catch-all gets a code of its own, and still leaks nothing.
    crashed = client.get("/api/v1/chat-channels/channel-1").json()
    assert crashed["retcode"] == int(RetCode.EXCEPTION_ERROR)
    assert crashed["data"] == {"error_code": "CHANNEL_OPERATION_FAILED"}
    assert "boom" not in str(crashed)


async def test_secret_rotation_increments_version_without_returning_secret() -> None:
    repository = FakeRepository()
    secret_store = FakeSecretStore()
    service = ChannelControlService(repository, secret_store)
    created = await service.create_channel("tenant-a", _create_request())

    updated = await service.update_channel(
        "tenant-a",
        created["id"],
        ChannelUpdateRequest.model_validate({"config": {"credential": {"app_secret": "replacement-secret"}}}),
    )

    assert updated["secret"] == {"configured": True, "version": 2}
    assert "replacement-secret" not in repr(updated)
    assert repository.secrets[created["id"]].ciphertext == "ciphertext-v2"


async def test_update_applies_connection_and_binding_in_one_transaction() -> None:
    repository = FakeRepository()
    repository.latest_canvas_revisions.add(("tenant-a", "agent-1", "revision-1"))
    secret_store = FakeSecretStore()
    service = ChannelControlService(repository, secret_store)
    created = await service.create_channel("tenant-a", _create_request())

    updated = await service.update_channel(
        "tenant-a",
        created["id"],
        ChannelUpdateRequest.model_validate(
            {
                "config": {
                    "credential": {
                        "app_id": "cli_replacement",
                        "app_secret": "replacement-secret",
                    }
                },
                "binding": {
                    "target_type": "multirag.canvas_agent",
                    "target_id": "agent-1",
                    "target_revision_id": "revision-1",
                    "enabled": False,
                },
            }
        ),
    )

    assert updated["config"]["credential"]["app_id"] == "cli_replacement"
    assert updated["binding"]["target_type"] == "multirag.canvas_agent"
    assert updated["binding"]["target_revision_id"] == "revision-1"
    assert updated["binding"]["enabled"] is False
    assert updated["secret"] == {"configured": True, "version": 2}
    assert "replacement-secret" not in repr(updated)


def test_update_rejects_conflicting_compatibility_and_binding_fields() -> None:
    with pytest.raises(ValidationError):
        ChannelUpdateRequest.model_validate(
            {
                "chat_id": "dialog-1",
                "binding": {
                    "target_type": "multirag.canvas_agent",
                    "target_id": "agent-1",
                    "target_revision_id": "revision-1",
                },
            }
        )


async def test_policy_rejects_embedded_credentials() -> None:
    repository = FakeRepository()
    repository.dialogs.add(("tenant-a", "dialog-1"))
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request())

    with pytest.raises(InvalidChannelConfiguration, match="must not contain credentials"):
        await service.upsert_binding(
            "tenant-a",
            created["id"],
            ChannelBindingUpsertRequest(
                target_type="multirag.dialog",
                target_id="dialog-1",
                policy={"api_token": "must-not-live-here"},
            ),
        )


async def test_second_enabled_channel_on_one_account_is_rejected_inside_a_tenant() -> None:
    """Per-binding leases (CHN-S3) removed the Redis-level guard against this.

    Two enabled bindings on one provider account would both connect and answer
    the same message twice, so the invariant moves to the control plane.
    """

    repository = FakeRepository()
    repository.dialogs.add(("tenant-a", "dialog-1"))
    service = ChannelControlService(repository, FakeSecretStore())
    first = await service.create_channel("tenant-a", _create_request(chat_id="dialog-1"))
    await service.set_enabled("tenant-a", first["id"], enabled=True)

    # Creating it is fine -- only enabling a second connection is not.
    second = await service.create_channel("tenant-a", _create_request(chat_id="dialog-1"))

    with pytest.raises(InvalidChannelConfiguration, match="already uses this provider account"):
        await service.set_enabled("tenant-a", second["id"], enabled=True)


async def test_two_tenants_may_enable_the_same_provider_account() -> None:
    """The uniqueness guard must not become a fresh cross-tenant squatting vector.

    A global check would let whoever registers an account id first lock every
    other tenant out of an account they may legitimately own -- reintroducing
    the class of bug CHN-S3 just closed.
    """

    repository = FakeRepository()
    repository.dialogs.add(("tenant-a", "dialog-a"))
    repository.dialogs.add(("tenant-b", "dialog-b"))
    service = ChannelControlService(repository, FakeSecretStore())
    first = await service.create_channel("tenant-a", _create_request(chat_id="dialog-a"))
    second = await service.create_channel("tenant-b", _create_request(chat_id="dialog-b"))

    await service.set_enabled("tenant-a", first["id"], enabled=True)
    await service.set_enabled("tenant-b", second["id"], enabled=True)

    assert repository.channels[first["id"]].status == 1
    assert repository.channels[second["id"]].status == 1


async def test_re_enabling_the_same_channel_is_not_a_self_conflict() -> None:
    repository = FakeRepository()
    repository.dialogs.add(("tenant-a", "dialog-1"))
    service = ChannelControlService(repository, FakeSecretStore())
    created = await service.create_channel("tenant-a", _create_request(chat_id="dialog-1"))

    await service.set_enabled("tenant-a", created["id"], enabled=True)
    again = await service.set_enabled("tenant-a", created["id"], enabled=True)

    assert again["status"] == 1


def test_sanitizer_strips_provider_credential_names_but_keeps_public_ids() -> None:
    """The read path must not echo credentials spelled the way providers spell them.

    The previous exact-match blocklist only knew the bare words (``secret``,
    ``token``, ...), so every real provider field sailed straight through into
    ``ChatChannelResponse.config``, which is returned to the browser.
    """

    sanitized = _sanitize_public_config(
        {
            "credential": {
                "app_id": "cli_aaaaaaaaaaaaaaaa",
                "corp_id": "ww_aaaaaaaaaaaaaaaa",
                "client_secret": "aaaa-aaaa-aaaa",
                "bot_token": "aaaa-aaaa-aaaa",
                "channel_access_token": "aaaa-aaaa-aaaa",
                "signing_secret": "aaaa-aaaa-aaaa",
                "aes_key": "a" * 43,
                "private_key": "aaaa-aaaa-aaaa",
                "cookies": "aaaa-aaaa-aaaa",
            },
            "domain": "feishu",
            "allowed_open_ids": ["ou_aaaa"],
        }
    )

    # Public identifiers survive -- they are what the management form redisplays.
    assert sanitized["credential"] == {
        "app_id": "cli_aaaaaaaaaaaaaaaa",
        "corp_id": "ww_aaaaaaaaaaaaaaaa",
    }
    assert sanitized["domain"] == "feishu"
    assert sanitized["allowed_open_ids"] == ["ou_aaaa"]


def test_sanitizer_leaves_non_secret_key_names_alone() -> None:
    """``key_id`` labels which master key encrypted a row -- it is not a secret.

    Guards the reason ``*_key`` is matched by suffix instead of by a bare
    ``key`` substring.
    """

    public = {"key_id": "aaaaaaaa", "keywords": ["a"], "monkey": "a"}

    assert _sanitize_public_config(public) == public


def test_policy_forbids_the_credential_block_that_config_may_carry() -> None:
    """The two predicates differ by exactly one key, deliberately.

    A binding policy carrying a credential block is always wrong; a public
    config legitimately holds ``credential.app_id``.
    """

    credential_block = {"credential": {"app_id": "cli_aaaaaaaaaaaaaaaa"}}

    assert _contains_sensitive_key(credential_block) is True
    assert _sanitize_public_config(credential_block) == credential_block
    # Provider-shaped names are caught inside nested policy structures too.
    assert _contains_sensitive_key({"nested": [{"client_secret": "aaaa"}]}) is True


async def test_unavailable_store_protocol_does_not_decrypt() -> None:
    with pytest.raises(SecretStoreUnavailable):
        await UnavailableSecretStore().decrypt(
            tenant_id="tenant-a",
            channel_id="channel-a",
            encrypted=EncryptedSecret(ciphertext="cipher", key_id="key", version=1),
        )


async def test_rotating_the_master_key_keeps_stored_credentials_readable() -> None:
    """CHN-O7 end to end, on the real cipher rather than the fake store.

    Rotation used to mean every tenant re-enters every credential, so the
    honest advice was "never rotate" -- which is not advice you can follow
    after a leak. A row remembers which key wrote it, so keeping the retired
    key on the ring keeps that row readable while new writes move to the new
    key. Dropping the retired key for real must fail closed with an error
    code, not hand a runner an empty credential.
    """

    retired = ChannelSecretCipher(os.urandom(32))
    repository = FakeRepository()
    repository.dialogs.add(("tenant-a", "dialog-1"))

    before_rotation = ChannelControlService(repository, AESGCMChannelSecretStore(retired))
    created = await before_rotation.create_channel("tenant-a", _create_request(chat_id="dialog-1"))
    await before_rotation.upsert_binding(
        "tenant-a",
        created["id"],
        ChannelBindingUpsertRequest(target_type="multirag.dialog", target_id="dialog-1"),
    )
    enabled = await before_rotation.set_enabled("tenant-a", created["id"], enabled=True)
    binding_id = enabled["binding"]["id"]

    rotated = ChannelControlService(repository, AESGCMChannelSecretStore(ChannelSecretCipher(os.urandom(32)), retired))
    resolved = await rotated.resolve_runtime_binding(binding_id)
    assert resolved.credentials["app_secret"] == "never-return-this-secret"

    retired_for_real = ChannelControlService(repository, AESGCMChannelSecretStore(ChannelSecretCipher(os.urandom(32))))
    with pytest.raises(ChannelCredentialUnavailable) as captured:
        await retired_for_real.resolve_runtime_binding(binding_id)
    assert captured.value.error_code == "CHANNEL_SECRET_STORE_UNAVAILABLE"


# --- credential self-check (CHN-O6) -----------------------------------------


class _StubVerifier:
    """Stands in for one provider's HTTP probe; records what it was handed."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[tuple[dict[str, str], dict[str, Any]]] = []

    async def verify_credential(self, *, credential: Mapping[str, str], public_config: Mapping[str, Any]) -> None:
        self.calls.append((dict(credential), dict(public_config)))
        if self._error is not None:
            raise self._error


async def _channel_awaiting_verification(
    monkeypatch: pytest.MonkeyPatch,
    verifier: object | None,
) -> tuple[ChannelControlService, FakeRepository, str]:
    repository = FakeRepository()
    service = ChannelControlService(repository, FakeSecretStore(), verification_throttle=VerificationThrottle())
    created = await service.create_channel("tenant-a", _create_request())
    monkeypatch.setattr("api.channel_control.service.credential_verifier", lambda name: verifier)
    return service, repository, created["id"]


async def test_verify_hands_the_provider_a_whole_credential_and_returns_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _StubVerifier()
    service, _, channel_id = await _channel_awaiting_verification(monkeypatch, verifier)

    result = await service.verify_channel_credential("tenant-a", channel_id)

    assert result == {"verified": True, "provider": "feishu"}
    # Same reassembly the runner gets: the encrypted half plus the non-secret
    # half that lives in the public config.
    credential, public_config = verifier.calls[0]
    assert credential["app_secret"] == "never-return-this-secret"
    assert credential["app_id"] == "cli_unit"
    assert "app_secret" not in repr(public_config)
    assert "never-return-this-secret" not in repr(result)


async def test_verify_passes_the_providers_own_rejection_code_through(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _StubVerifier(ChannelCredentialRejected("CHANNEL_CREDENTIAL_INCOMPLETE"))
    service, _, channel_id = await _channel_awaiting_verification(monkeypatch, verifier)

    with pytest.raises(ChannelVerificationRejected) as captured:
        await service.verify_channel_credential("tenant-a", channel_id)

    assert captured.value.error_code == "CHANNEL_CREDENTIAL_INCOMPLETE"


async def test_verify_reports_an_unreachable_provider_as_inconclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The distinction the endpoint lives or dies by.

    Calling a timeout a rejection would send an admin to re-enter a credential
    that was correct the whole time -- the exact wasted loop CHN-O6 removes.
    """

    verifier = _StubVerifier(ChannelVerificationUnavailable())
    service, _, channel_id = await _channel_awaiting_verification(monkeypatch, verifier)

    with pytest.raises(ChannelVerificationInconclusive) as captured:
        await service.verify_channel_credential("tenant-a", channel_id)

    assert captured.value.error_code == "CHANNEL_VERIFICATION_UNAVAILABLE"


async def test_verify_refuses_a_second_attempt_inside_the_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """One request in, one third-party call out -- so it has to be bounded."""

    verifier = _StubVerifier()
    service, _, channel_id = await _channel_awaiting_verification(monkeypatch, verifier)

    await service.verify_channel_credential("tenant-a", channel_id)
    with pytest.raises(ChannelVerificationThrottled):
        await service.verify_channel_credential("tenant-a", channel_id)

    assert len(verifier.calls) == 1


async def test_verify_on_a_provider_without_a_probe_says_so_instead_of_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not every transport can cheaply answer this, and that is not an error."""

    service, _, channel_id = await _channel_awaiting_verification(monkeypatch, None)

    with pytest.raises(ChannelVerificationNotSupported) as captured:
        await service.verify_channel_credential("tenant-a", channel_id)

    assert captured.value.error_code == "CHANNEL_VERIFICATION_NOT_SUPPORTED"


async def test_verify_is_tenant_scoped_like_every_other_channel_route(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, channel_id = await _channel_awaiting_verification(monkeypatch, _StubVerifier())

    with pytest.raises(ChannelAccessDenied):
        await service.verify_channel_credential("tenant-b", channel_id)


async def test_verify_without_a_stored_credential_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _StubVerifier()
    service, repository, channel_id = await _channel_awaiting_verification(monkeypatch, verifier)
    repository.secrets.pop(channel_id)

    with pytest.raises(InvalidChannelConfiguration):
        await service.verify_channel_credential("tenant-a", channel_id)

    assert verifier.calls == []


def test_throttle_admits_once_per_window_then_reopens() -> None:
    throttle = VerificationThrottle(cooldown_seconds=10.0)

    assert throttle.admit(tenant_id="tenant-a", channel_id="channel-a", now=100.0) is True
    assert throttle.admit(tenant_id="tenant-a", channel_id="channel-a", now=105.0) is False
    # A different channel is a different bucket; one noisy admin must not lock
    # out everybody else's self-check.
    assert throttle.admit(tenant_id="tenant-a", channel_id="channel-b", now=105.0) is True
    assert throttle.admit(tenant_id="tenant-a", channel_id="channel-a", now=111.0) is True


def test_verify_route_separates_rejected_from_unreachable_from_throttled(client) -> None:
    """Three outcomes, three retcodes. Collapsing them re-creates CHN-U1."""

    outcomes = iter(
        [
            ChannelVerificationRejected(),
            ChannelVerificationInconclusive(),
            ChannelVerificationThrottled(),
        ]
    )

    class StubService:
        async def verify_channel_credential(self, tenant_id: str, channel_id: str) -> dict[str, Any]:
            del tenant_id, channel_id
            raise next(outcomes)

    client.app.dependency_overrides[get_channel_control_service] = StubService

    rejected = client.post("/api/v1/chat-channels/channel-1/verify").json()
    assert rejected["retcode"] == int(RetCode.ARGUMENT_ERROR)
    assert rejected["data"] == {"error_code": "CHANNEL_CREDENTIAL_REJECTED"}

    inconclusive = client.post("/api/v1/chat-channels/channel-1/verify").json()
    assert inconclusive["retcode"] == int(RetCode.CONNECTION_ERROR)
    assert inconclusive["data"] == {"error_code": "CHANNEL_VERIFICATION_UNAVAILABLE"}

    throttled = client.post("/api/v1/chat-channels/channel-1/verify").json()
    assert throttled["retcode"] == int(RetCode.RESOURCE_EXHAUSTED)
    assert throttled["data"] == {"error_code": "CHANNEL_VERIFICATION_THROTTLED"}
