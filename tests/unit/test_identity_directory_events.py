"""Unit contracts for EIM-I7 mapping and cache acceleration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from api.channels.identity_events import (
    ChannelIdentityEvent,
    ChannelIdentityIdentifier,
    ChannelIdentitySubject,
)
from api.identity.contracts import ProviderContext
from api.identity.directory_events import (
    DirectoryEventCommand,
    DirectoryEventError,
    DirectoryEventErrorCode,
    DirectoryEventIdentifier,
    DirectoryEventOutcome,
    DirectoryEventProcessingResult,
    DirectoryEventType,
    DirectoryStatus,
    canonical_directory_event_hash,
)
from api.identity.providers.contracts import ProviderIdentifierKind
from api.identity_adapters.channel_directory_events import ChannelDirectoryEventService
from api.identity_adapters.channel_runtime import IdentityProviderRegistry

_NOW = datetime(2026, 8, 24, 6, 0, tzinfo=UTC)


def _event() -> ChannelIdentityEvent:
    return ChannelIdentityEvent(
        event_type="contact.user.updated_v3",
        event_id="directory-event-1",
        event_at=_NOW,
        observed_app_id="app-observed",
        observed_tenant_key="tenant-observed",
        subject=ChannelIdentitySubject(
            identifiers=(
                ChannelIdentityIdentifier(kind="open_id", value="ou-observed"),
                ChannelIdentityIdentifier(kind="user_id", value="user-observed"),
            ),
            directory_status="inactive",
        ),
    )


def _context() -> ProviderContext:
    return ProviderContext(
        tenant_id="tenant-platform",
        provider="feishu",
        provider_tenant_key="tenant-observed",
        provider_account_id="account-server",
        provider_account_key="app-observed",
        provider_account_revision=2,
    )


class _Repository:
    def __init__(self, result: DirectoryEventProcessingResult) -> None:
        self.result = result
        self.commands: list[DirectoryEventCommand] = []
        self.committed = False

    async def process(
        self,
        command: DirectoryEventCommand,
    ) -> DirectoryEventProcessingResult:
        self.commands.append(command)
        self.committed = True
        return self.result


class _Provider:
    def __init__(self, repository: _Repository, *, fail: bool = False) -> None:
        self.repository = repository
        self.fail = fail
        self.invalidations: list[ProviderContext] = []

    async def resolve(self, *_args: Any, **_kwargs: Any) -> object:
        return object()

    async def refresh(self, *_args: Any, **_kwargs: Any) -> object:
        return object()

    async def invalidate(self, context: ProviderContext) -> None:
        assert self.repository.committed
        self.invalidations.append(context)
        if self.fail:
            raise RuntimeError("cache acceleration failed")


@pytest.mark.parametrize("event_id", ["事件一", "event one", "event\nline"])
def test_channel_event_id_is_safe_for_http_header(event_id: str) -> None:
    payload = _event().model_dump(mode="python")
    payload["event_id"] = event_id

    with pytest.raises(ValidationError):
        ChannelIdentityEvent.model_validate(payload)


def test_canonical_hash_is_identifier_order_independent_and_binds_observed_proof() -> None:
    identifiers = (
        DirectoryEventIdentifier(ProviderIdentifierKind.OPEN_ID, "ou-observed"),
        DirectoryEventIdentifier(ProviderIdentifierKind.USER_ID, "user-observed"),
    )
    command = DirectoryEventCommand(
        binding_id="binding-1",
        binding_generation=7,
        event_type=DirectoryEventType.USER_UPDATED,
        event_id="directory-event-1",
        event_at=_NOW,
        observed_app_id="app-observed",
        observed_tenant_key="tenant-observed",
        identifiers=identifiers,
        directory_status=DirectoryStatus.INACTIVE,
    )

    assert canonical_directory_event_hash(command) == canonical_directory_event_hash(
        DirectoryEventCommand(
            binding_id=command.binding_id,
            binding_generation=command.binding_generation,
            event_type=command.event_type,
            event_id=command.event_id,
            event_at=command.event_at,
            observed_app_id=command.observed_app_id,
            observed_tenant_key=command.observed_tenant_key,
            identifiers=tuple(reversed(identifiers)),
            directory_status=command.directory_status,
        )
    )
    changed_app = DirectoryEventCommand(
        binding_id=command.binding_id,
        binding_generation=command.binding_generation,
        event_type=command.event_type,
        event_id=command.event_id,
        event_at=command.event_at,
        observed_app_id="different-app",
        observed_tenant_key=command.observed_tenant_key,
        identifiers=command.identifiers,
        directory_status=command.directory_status,
    )
    assert canonical_directory_event_hash(command) != canonical_directory_event_hash(changed_app)


async def test_service_invalidates_only_after_durable_revision_bump() -> None:
    repository = _Repository(
        DirectoryEventProcessingResult(
            outcome=DirectoryEventOutcome.APPLIED,
            context=_context(),
            revision_bumped=True,
        )
    )
    provider = _Provider(repository)
    registry = IdentityProviderRegistry({"feishu": lambda: provider})
    service = ChannelDirectoryEventService(
        repository,
        registry,
    )

    result = await service.receive(
        binding_id="binding-1",
        binding_generation=7,
        event=_event(),
    )

    assert result.outcome is DirectoryEventOutcome.APPLIED
    assert provider.invalidations == [_context()]
    command = repository.commands[0]
    assert command.observed_app_id == "app-observed"
    assert command.observed_tenant_key == "tenant-observed"


async def test_cache_invalidation_failure_does_not_undo_committed_event() -> None:
    repository = _Repository(
        DirectoryEventProcessingResult(
            outcome=DirectoryEventOutcome.APPLIED,
            context=_context(),
            revision_bumped=True,
        )
    )
    provider = _Provider(repository, fail=True)
    service = ChannelDirectoryEventService(
        repository,
        IdentityProviderRegistry({"feishu": lambda: provider}),
    )

    result = await service.receive(
        binding_id="binding-1",
        binding_generation=7,
        event=_event(),
    )

    assert result.outcome is DirectoryEventOutcome.APPLIED
    assert provider.invalidations == [_context()]


async def test_provider_construction_failure_does_not_undo_committed_event() -> None:
    repository = _Repository(
        DirectoryEventProcessingResult(
            outcome=DirectoryEventOutcome.APPLIED,
            context=_context(),
            revision_bumped=True,
        )
    )

    def fail_provider_construction() -> _Provider:
        raise RuntimeError("provider construction failed")

    service = ChannelDirectoryEventService(
        repository,
        IdentityProviderRegistry({"feishu": fail_provider_construction}),
    )

    result = await service.receive(
        binding_id="binding-1",
        binding_generation=7,
        event=_event(),
    )

    assert result.outcome is DirectoryEventOutcome.APPLIED


async def test_deterministic_failed_event_invalidates_then_returns_terminal_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = _Repository(
        DirectoryEventProcessingResult(
            outcome=DirectoryEventOutcome.FAILED,
            context=_context(),
            revision_bumped=True,
            error_code=DirectoryEventErrorCode.IDENTITY_CONFLICT,
        )
    )
    provider = _Provider(repository)
    service = ChannelDirectoryEventService(
        repository,
        IdentityProviderRegistry({"feishu": lambda: provider}),
    )

    result = await service.receive(
        binding_id="binding-1",
        binding_generation=7,
        event=_event(),
    )

    assert result.error_code is DirectoryEventErrorCode.IDENTITY_CONFLICT
    assert provider.invalidations == [_context()]
    assert "error_code=IDENTITY_DIRECTORY_IDENTITY_CONFLICT" in caplog.text
    assert "directory-event-1" not in caplog.text


async def test_processing_receipt_remains_retryable() -> None:
    repository = _Repository(
        DirectoryEventProcessingResult(
            outcome=DirectoryEventOutcome.FAILED,
            context=_context(),
            error_code=DirectoryEventErrorCode.RECEIPT_PROCESSING,
        )
    )
    service = ChannelDirectoryEventService(
        repository,
        IdentityProviderRegistry({}),
    )

    with pytest.raises(DirectoryEventError) as captured:
        await service.receive(
            binding_id="binding-1",
            binding_generation=7,
            event=_event(),
        )

    assert captured.value.code is DirectoryEventErrorCode.RECEIPT_PROCESSING
