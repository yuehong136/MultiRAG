"""Channel control-plane contracts, isolation, and credential safety."""

from __future__ import annotations

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
from api.channel_control.secret_store import EncryptedSecret, SecretStoreUnavailable, UnavailableSecretStore
from api.channel_control.service import (
    ChannelAccessDenied,
    ChannelControlService,
    ChannelCredentialUnavailable,
    InvalidChannelConfiguration,
)
from api.db.db_models import ChannelBinding, ChannelRuntimeStatus, ChannelSecret, ChatChannel
from common.app_config import get_app_config
from common.constants import RetCode


class FakeRepository:
    def __init__(self) -> None:
        self.channels: dict[str, ChatChannel] = {}
        self.secrets: dict[str, ChannelSecret] = {}
        self.bindings: dict[str, ChannelBinding] = {}
        self.runtimes: dict[str, ChannelRuntimeStatus] = {}
        self.dialogs: set[tuple[str, str]] = set()
        self.latest_canvas_revisions: set[tuple[str, str, str]] = set()
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

    async def dialog_belongs_to_tenant(self, tenant_id: str, dialog_id: str) -> bool:
        return (tenant_id, dialog_id) in self.dialogs

    async def canvas_revision_is_latest_published(
        self,
        tenant_id: str,
        canvas_id: str,
        revision_id: str,
    ) -> bool:
        return (tenant_id, canvas_id, revision_id) in self.latest_canvas_revisions

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
        del tenant_id, channel_id, encrypted
        return {}


def _create_request(*, chat_id: str | None = None, status: int = 0) -> ChannelCreateRequest:
    return ChannelCreateRequest.model_validate(
        {
            "name": "Leadership demo",
            "channel": "feishu",
            "config": {
                "credential": {
                    "app_id": "cli_unit",
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
    # The hint is read-only: it must not rebind, advance generation or fake runtime.
    assert stale["binding"]["target_revision_id"] == "revision-1"
    assert stale["binding"]["generation"] == fresh["binding"]["generation"]
    assert stale["generation"] == fresh["generation"]
    assert stale["runtime"] == fresh["runtime"]

    listed = await service.list_channels("tenant-a")
    assert [item["binding"]["revision_stale"] for item in listed["items"]] == [True]


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
    assert provider_body["data"]["items"][0]["provider"] == "feishu"
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


async def test_unavailable_store_protocol_does_not_decrypt() -> None:
    with pytest.raises(SecretStoreUnavailable):
        await UnavailableSecretStore().decrypt(
            tenant_id="tenant-a",
            channel_id="channel-a",
            encrypted=EncryptedSecret(ciphertext="cipher", key_id="key", version=1),
        )
