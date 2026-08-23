"""Compose Channel identity DTOs with the durable directory-event service."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from api.channels.identity_events import ChannelIdentityEvent
from api.identity.contracts import ProviderContext
from api.identity.directory_events import (
    DirectoryEventCommand,
    DirectoryEventError,
    DirectoryEventErrorCode,
    DirectoryEventIdentifier,
    DirectoryEventProcessingResult,
    DirectoryEventRepository,
    DirectoryEventType,
    DirectoryStatus,
)
from api.identity.providers.contracts import ProviderIdentifierKind
from api.identity_adapters.channel_runtime import IdentityProviderRegistry

LOGGER = logging.getLogger(__name__)


@runtime_checkable
class ProviderCacheInvalidator(Protocol):
    """Optional acceleration; account revision remains the durable fence."""

    async def invalidate(self, context: ProviderContext) -> None: ...


class ChannelDirectoryEventService:
    """Map untrusted observed headers, commit, then accelerate local eviction."""

    def __init__(
        self,
        repository: DirectoryEventRepository,
        provider_registry: IdentityProviderRegistry,
    ) -> None:
        self._repository = repository
        self._provider_registry = provider_registry

    async def receive(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        event: ChannelIdentityEvent,
    ) -> DirectoryEventProcessingResult:
        command = _map_event(
            binding_id=binding_id,
            binding_generation=binding_generation,
            event=event,
        )
        result = await self._repository.process(command)
        if result.revision_bumped and result.context is not None:
            await self._invalidate_provider_cache(result.context)
        if result.error_code is DirectoryEventErrorCode.RECEIPT_PROCESSING:
            raise DirectoryEventError(result.error_code)
        log = LOGGER.warning if result.error_code is not None else LOGGER.info
        log(
            "identity_directory_event=processed result=%s error_code=%s",
            result.outcome.value,
            result.error_code.value if result.error_code is not None else "NONE",
        )
        return result

    async def _invalidate_provider_cache(self, context: ProviderContext) -> None:
        try:
            provider = self._provider_registry.get(context.provider)
            if not isinstance(provider, ProviderCacheInvalidator):
                LOGGER.warning(
                    "identity_directory_event=cache_invalidation result=skipped error_code=IDENTITY_PROVIDER_INVALIDATOR_UNAVAILABLE",
                )
                return
            await provider.invalidate(context)
        except Exception:
            LOGGER.warning(
                "identity_directory_event=cache_invalidation result=failed error_code=IDENTITY_PROVIDER_INVALIDATION_FAILED",
            )


def _map_event(
    *,
    binding_id: str,
    binding_generation: int,
    event: ChannelIdentityEvent,
) -> DirectoryEventCommand:
    subject = event.subject
    return DirectoryEventCommand(
        binding_id=binding_id,
        binding_generation=binding_generation,
        event_type=DirectoryEventType(event.event_type),
        event_id=event.event_id,
        event_at=event.event_at,
        observed_app_id=event.observed_app_id,
        observed_tenant_key=event.observed_tenant_key,
        identifiers=()
        if subject is None
        else tuple(
            DirectoryEventIdentifier(
                kind=ProviderIdentifierKind(identifier.kind),
                value=identifier.value,
            )
            for identifier in subject.identifiers
        ),
        directory_status=(None if subject is None else DirectoryStatus(subject.directory_status)),
    )
