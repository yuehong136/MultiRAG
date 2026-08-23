"""Private service-to-service execution API for Channel runtimes.

The repository's route discovery mounts REST modules below ``/api/v1``. The
resulting private endpoint is therefore
``/api/v1/internal/channel-bindings/{binding_id}/executions``. The explicit
``internal`` segment preserves the trust-boundary semantics without changing
the shared application bootstrap solely for this endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.channel_capabilities import ChannelRuntimeCapabilities, RunCapabilityPolicy, resolve_effective_reply_capabilities
from api.channel_control.repository import SqlAlchemyChannelRepository
from api.channel_execution.candidate_gc import build_channel_candidate_gc_worker
from api.channel_execution.dependencies import (
    build_interaction_callback_processor,
    get_binding_capability_resolver,
    get_channel_conversation_store,
    get_channel_execution_service,
    get_published_target_execution_service,
    require_channel_workload,
)
from api.channel_execution.errors import BindingDisabledError, BindingNotFoundError, ChannelExecutionError, DuplicateEventError
from api.channel_execution.interaction_worker import (
    InteractionCallbackProcessor,
    run_interaction_callback_worker,
)
from api.channel_execution.models import ChannelExecutionCommand, ExecutionEvent, WorkloadIdentity
from api.channel_execution.protocols import BindingCapabilityResolver, ChannelConversationStore
from api.channel_execution.service import ChannelExecutionService, PublishedTargetExecutionService
from api.channel_providers.registry import UnknownChannelProvider, provider_spec
from api.db.db_models import get_async_db
from common.app_config import get_app_config

LOGGER = logging.getLogger(__name__)
_CANDIDATE_GC_STATE_KEY = "_multirag_channel_candidate_gc"
_INTERACTION_CALLBACK_STATE_KEY = "_multirag_channel_interaction_callback"


@dataclass(slots=True)
class _CandidateGCLifecycleHandle:
    stop_event: asyncio.Event
    task: asyncio.Task[None]


@dataclass(slots=True)
class _InteractionCallbackLifecycleHandle:
    stop_event: asyncio.Event
    task: asyncio.Task[None]


def _observe_candidate_gc_task_exit(
    task: asyncio.Task[None],
    *,
    stop_event: asyncio.Event,
) -> None:
    """Consume and report an unexpected terminal task state immediately."""

    if stop_event.is_set():
        return
    if task.cancelled():
        LOGGER.error(
            "channel_execution_event=candidate_gc_task_exit result=failed error_code=CANDIDATE_GC_TASK_CANCELLED",
        )
        return
    error = task.exception()
    if error is None:
        LOGGER.error(
            "channel_execution_event=candidate_gc_task_exit result=failed error_code=CANDIDATE_GC_TASK_STOPPED",
        )
        return
    LOGGER.error(
        "channel_execution_event=candidate_gc_task_exit result=failed error_code=CANDIDATE_GC_TASK_CRASHED error_type=%s",
        type(error).__name__,
    )


@asynccontextmanager
async def _candidate_gc_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own exactly one API-local candidate collector for this application."""

    existing = getattr(app.state, _CANDIDATE_GC_STATE_KEY, None)
    if isinstance(existing, _CandidateGCLifecycleHandle) and not existing.task.done():
        # Defensive only: current route discovery includes this router once, but
        # a future nested router must not double the database sweep.
        yield
        return

    worker = build_channel_candidate_gc_worker()
    if worker is None:
        yield
        return

    stop_event = asyncio.Event()
    task = asyncio.create_task(
        worker.run(stop_event),
        name="multirag-channel-candidate-gc",
    )
    task.add_done_callback(
        lambda completed: _observe_candidate_gc_task_exit(
            completed,
            stop_event=stop_event,
        )
    )
    handle = _CandidateGCLifecycleHandle(stop_event, task)
    setattr(app.state, _CANDIDATE_GC_STATE_KEY, handle)
    LOGGER.info(
        "channel_execution_event=candidate_gc_start result=ok error_code=",
    )
    try:
        yield
    finally:
        stop_event.set()
        task.cancel()
        stopped_cleanly = True
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            stopped_cleanly = False
            LOGGER.warning(
                "channel_execution_event=candidate_gc_stop result=failed error_code=CANDIDATE_GC_STOP_FAILED error_type=%s",
                type(exc).__name__,
            )
        if getattr(app.state, _CANDIDATE_GC_STATE_KEY, None) is handle:
            delattr(app.state, _CANDIDATE_GC_STATE_KEY)
        if stopped_cleanly:
            LOGGER.info(
                "channel_execution_event=candidate_gc_stop result=ok error_code=",
            )


def _observe_interaction_callback_task_exit(
    task: asyncio.Task[None],
    *,
    stop_event: asyncio.Event,
) -> None:
    """Report an API-local callback worker that terminates unexpectedly."""

    if stop_event.is_set():
        return
    if task.cancelled():
        error_code = "INTERACTION_CALLBACK_TASK_CANCELLED"
        error_type = ""
    else:
        error = task.exception()
        error_code = "INTERACTION_CALLBACK_TASK_STOPPED" if error is None else "INTERACTION_CALLBACK_TASK_CRASHED"
        error_type = "" if error is None else f" error_type={type(error).__name__}"
    LOGGER.error(
        "channel_execution_event=interaction_callback_task_exit result=failed error_code=%s%s",
        error_code,
        error_type,
    )


def _build_interaction_callback_runtime() -> tuple[InteractionCallbackProcessor, float] | None:
    processor = build_interaction_callback_processor()
    if processor is None:
        return None
    return processor, float(get_app_config().identity.mcp_interactions.poll_seconds)


@asynccontextmanager
async def _interaction_callback_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own one callback receipt processor per API worker process."""

    existing = getattr(app.state, _INTERACTION_CALLBACK_STATE_KEY, None)
    if isinstance(existing, _InteractionCallbackLifecycleHandle) and not existing.task.done():
        yield
        return

    runtime = _build_interaction_callback_runtime()
    if runtime is None:
        yield
        return
    processor, poll_seconds = runtime
    stop_event = asyncio.Event()
    owner = f"api-{os.getpid()}-{uuid.uuid4().hex[:16]}"
    task = asyncio.create_task(
        run_interaction_callback_worker(
            processor=processor,
            owner=owner,
            poll_seconds=poll_seconds,
            stopping=stop_event,
        ),
        name="multirag-channel-interaction-callback",
    )
    task.add_done_callback(
        lambda completed: _observe_interaction_callback_task_exit(
            completed,
            stop_event=stop_event,
        )
    )
    handle = _InteractionCallbackLifecycleHandle(stop_event, task)
    setattr(app.state, _INTERACTION_CALLBACK_STATE_KEY, handle)
    LOGGER.info(
        "channel_execution_event=interaction_callback_start result=ok error_code=",
    )
    try:
        yield
    finally:
        stop_event.set()
        task.cancel()
        stopped_cleanly = True
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            stopped_cleanly = False
            LOGGER.warning(
                "channel_execution_event=interaction_callback_stop result=failed error_code=INTERACTION_CALLBACK_STOP_FAILED error_type=%s",
                type(exc).__name__,
            )
        if getattr(app.state, _INTERACTION_CALLBACK_STATE_KEY, None) is handle:
            delattr(app.state, _INTERACTION_CALLBACK_STATE_KEY)
        if stopped_cleanly:
            LOGGER.info(
                "channel_execution_event=interaction_callback_stop result=ok error_code=",
            )


@asynccontextmanager
async def _channel_execution_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own API-local Channel maintenance workers without double starts."""

    async with _candidate_gc_lifespan(app), _interaction_callback_lifespan(app):
        yield


router = APIRouter(lifespan=_channel_execution_lifespan)


def _encode_sse(event: ExecutionEvent) -> str:
    payload = event.model_dump(mode="json", exclude_none=True)
    return "data:" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


@router.get(
    "/internal/channel-bindings/{binding_id}/execution-capabilities",
    response_model=ChannelRuntimeCapabilities,
    include_in_schema=False,
)
async def get_channel_execution_capabilities(
    response: Response,
    binding_id: str = Path(min_length=1, max_length=32),
    workload: WorkloadIdentity = Depends(require_channel_workload),
    resolver: BindingCapabilityResolver = Depends(get_binding_capability_resolver),
    target_service: PublishedTargetExecutionService = Depends(get_published_target_execution_service),
) -> ChannelRuntimeCapabilities:
    """Resolve one sanitized capability envelope per binding generation."""

    context = await resolver.resolve_capabilities(
        binding_id=binding_id,
        workload=workload,
    )
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="BINDING_NOT_FOUND")
    if not context.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="BINDING_DISABLED")
    try:
        target = await target_service.capabilities(context=context)
        provider = provider_spec(context.provider).capabilities
    except (ChannelExecutionError, UnknownChannelProvider) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="EXECUTION_CAPABILITIES_UNAVAILABLE") from exc
    response.headers["Cache-Control"] = "private, no-store"
    reply_capabilities = resolve_effective_reply_capabilities(
        provider,
        target,
        RunCapabilityPolicy.from_binding_policy(context.run_policy),
    )
    return ChannelRuntimeCapabilities.model_validate(
        {
            **reply_capabilities.model_dump(),
            "identity_event_receipt": True,
        }
    )


@router.post(
    "/internal/channel-bindings/{binding_id}/executions",
    summary="Execute a trusted Channel binding",
    response_class=StreamingResponse,
)
async def execute_channel_binding(
    command: ChannelExecutionCommand,
    binding_id: str = Path(min_length=1, max_length=255),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    workload: WorkloadIdentity = Depends(require_channel_workload),
    service: ChannelExecutionService = Depends(get_channel_execution_service),
) -> StreamingResponse:
    """Execute only the target and tenant resolved by trusted binding state."""

    if idempotency_key != command.event_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must match event_id.",
        )

    try:
        events = await service.execute(
            binding_id=binding_id,
            workload=workload,
            command=command,
        )
    except BindingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.code) from exc
    except BindingDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc
    except DuplicateEventError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc

    async def generate() -> AsyncIterator[str]:
        async for event in events:
            yield _encode_sse(event)
        yield "data:[DONE]\n\n"

    response = StreamingResponse(generate(), media_type="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Connection"] = "keep-alive"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Content-Type"] = "text/event-stream; charset=utf-8"
    return response


@router.delete(
    "/internal/channel-bindings/{binding_id}/conversations/{conversation_key}",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
async def reset_channel_conversation(
    binding_id: str = Path(min_length=1, max_length=32),
    conversation_key: str = Path(min_length=1, max_length=512),
    workload: WorkloadIdentity = Depends(require_channel_workload),
    store: ChannelConversationStore = Depends(get_channel_conversation_store),
    db: AsyncSession = Depends(get_async_db),
) -> Response:
    """Reset only a server-resolved active binding conversation."""

    if workload.binding_id != binding_id or workload.binding_generation is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized channel runtime.")
    bundle = await SqlAlchemyChannelRepository(db).get_runtime_binding(binding_id)
    if bundle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="BINDING_NOT_FOUND")
    channel, binding, _secret = bundle
    if channel.status != 1 or not binding.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="BINDING_DISABLED")
    if workload.binding_generation != binding.generation:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="BINDING_GENERATION_STALE")
    try:
        await store.reset_session(
            binding_id=binding_id,
            binding_generation=binding.generation,
            conversation_key=conversation_key,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CHANNEL_STATE_UNAVAILABLE",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
