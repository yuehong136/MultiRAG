"""Trusted orchestration services for Channel-originated target execution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import replace

from api.channel_execution.errors import BindingDisabledError, BindingNotFoundError, ChannelExecutionError, DuplicateEventError
from api.channel_execution.models import ChannelExecutionCommand, ExecutionEvent, TrustedChannelContext, WorkloadIdentity
from api.channel_execution.protocols import BindingResolver, ChannelConversationStore, ExecutionClaimStore
from api.channel_execution.registry import TargetExecutorRegistry

LOGGER = logging.getLogger(__name__)


async def _one_failure(code: str) -> AsyncIterator[ExecutionEvent]:
    yield ExecutionEvent(event="execution_failed", error_code=code)


class PublishedTargetExecutionService:
    """Dispatches trusted target references to a registered MultiRAG executor."""

    def __init__(self, registry: TargetExecutorRegistry) -> None:
        self._registry = registry

    async def execute(
        self,
        *,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> AsyncIterator[ExecutionEvent]:
        try:
            executor = self._registry.get(context.target.target_type)
            events = await executor.execute(context=context, command=command)
        except asyncio.CancelledError:
            raise
        except ChannelExecutionError as exc:
            return _one_failure(exc.code)
        except Exception as exc:
            LOGGER.warning(
                "channel_execution_event=target_prepare_failed error_type=%s",
                type(exc).__name__,
            )
            return _one_failure("TARGET_EXECUTION_FAILED")
        return self._sanitize(events)

    async def _sanitize(self, events: AsyncIterator[ExecutionEvent]) -> AsyncIterator[ExecutionEvent]:
        try:
            async for event in events:
                # Re-validation prevents custom executors from leaking arbitrary
                # dictionaries or extra internal fields through this boundary.
                yield ExecutionEvent.model_validate(event.model_dump())
        except asyncio.CancelledError:
            raise
        except ChannelExecutionError as exc:
            yield ExecutionEvent(event="execution_failed", error_code=exc.code)
        except Exception as exc:
            LOGGER.warning(
                "channel_execution_event=target_stream_failed error_type=%s",
                type(exc).__name__,
            )
            yield ExecutionEvent(event="execution_failed", error_code="TARGET_EXECUTION_FAILED")


class ChannelExecutionService:
    """Resolves trusted binding state before invoking the shared target service."""

    def __init__(
        self,
        *,
        binding_resolver: BindingResolver,
        conversation_store: ChannelConversationStore,
        claim_store: ExecutionClaimStore,
        target_service: PublishedTargetExecutionService,
    ) -> None:
        self._binding_resolver = binding_resolver
        self._conversation_store = conversation_store
        self._claim_store = claim_store
        self._target_service = target_service

    async def execute(
        self,
        *,
        binding_id: str,
        workload: WorkloadIdentity,
        command: ChannelExecutionCommand,
    ) -> AsyncIterator[ExecutionEvent]:
        context = await self._binding_resolver.resolve(
            binding_id=binding_id,
            workload=workload,
            command=command,
        )
        if context is None or context.binding_id != binding_id:
            raise BindingNotFoundError()
        if not context.enabled:
            raise BindingDisabledError()
        try:
            claimed = await self._claim_store.claim(binding_id=binding_id, event_id=command.event_id)
        except asyncio.CancelledError:
            raise
        except ChannelExecutionError as exc:
            return _one_failure(exc.code)
        except Exception as exc:
            LOGGER.warning(
                "channel_execution_event=event_claim_failed error_type=%s",
                type(exc).__name__,
            )
            return _one_failure("CHANNEL_STATE_UNAVAILABLE")
        if not claimed:
            raise DuplicateEventError()

        try:
            session_id = await self._conversation_store.get_session(
                binding_id=binding_id,
                binding_generation=context.binding_generation,
                conversation_key=command.conversation_key,
            )
        except asyncio.CancelledError:
            raise
        except ChannelExecutionError as exc:
            await self._fail_claim(binding_id=binding_id, event_id=command.event_id)
            return _one_failure(exc.code)
        except Exception as exc:
            LOGGER.warning(
                "channel_execution_event=session_load_failed error_type=%s",
                type(exc).__name__,
            )
            await self._fail_claim(binding_id=binding_id, event_id=command.event_id)
            return _one_failure("CHANNEL_STATE_UNAVAILABLE")

        trusted_context = replace(context, session_id=session_id)
        events = await self._target_service.execute(context=trusted_context, command=command)
        return self._persist_completed_session(
            events,
            binding_id=binding_id,
            binding_generation=context.binding_generation,
            conversation_key=command.conversation_key,
            event_id=command.event_id,
        )

    async def _persist_completed_session(
        self,
        events: AsyncIterator[ExecutionEvent],
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
        event_id: str,
    ) -> AsyncIterator[ExecutionEvent]:
        try:
            async for event in events:
                if event.event == "message_completed":
                    if not event.session_id:
                        await self._fail_claim(binding_id=binding_id, event_id=event_id)
                        yield ExecutionEvent(event="execution_failed", error_code="TARGET_EXECUTION_FAILED")
                        return
                    await self._conversation_store.put_session(
                        binding_id=binding_id,
                        binding_generation=binding_generation,
                        conversation_key=conversation_key,
                        session_id=event.session_id,
                    )
                    await self._claim_store.complete(binding_id=binding_id, event_id=event_id)
                    yield event
                    return
                if event.event == "execution_failed":
                    await self._fail_claim(binding_id=binding_id, event_id=event_id)
                    yield event
                    return
                yield event
            await self._fail_claim(binding_id=binding_id, event_id=event_id)
            yield ExecutionEvent(event="execution_failed", error_code="TARGET_EXECUTION_FAILED")
        except asyncio.CancelledError:
            await self._fail_claim(binding_id=binding_id, event_id=event_id)
            raise
        except ChannelExecutionError as exc:
            await self._fail_claim(binding_id=binding_id, event_id=event_id)
            yield ExecutionEvent(event="execution_failed", error_code=exc.code)
        except Exception as exc:
            LOGGER.warning(
                "channel_execution_event=session_save_failed error_type=%s",
                type(exc).__name__,
            )
            await self._fail_claim(binding_id=binding_id, event_id=event_id)
            yield ExecutionEvent(event="execution_failed", error_code="CHANNEL_STATE_UNAVAILABLE")

    async def _fail_claim(self, *, binding_id: str, event_id: str) -> None:
        try:
            await self._claim_store.fail(binding_id=binding_id, event_id=event_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning(
                "channel_execution_event=event_fail_mark_failed error_type=%s",
                type(exc).__name__,
            )
