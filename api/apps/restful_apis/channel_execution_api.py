"""Private service-to-service execution API for Channel runtimes.

The repository's route discovery mounts REST modules below ``/api/v1``. The
resulting private endpoint is therefore
``/api/v1/internal/channel-bindings/{binding_id}/executions``. The explicit
``internal`` segment preserves the trust-boundary semantics without changing
the shared application bootstrap solely for this endpoint.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.channel_control.repository import SqlAlchemyChannelRepository
from api.channel_execution.dependencies import (
    get_channel_conversation_store,
    get_channel_execution_service,
    require_channel_workload,
)
from api.channel_execution.errors import BindingDisabledError, BindingNotFoundError, DuplicateEventError
from api.channel_execution.models import ChannelExecutionCommand, ExecutionEvent, WorkloadIdentity
from api.channel_execution.protocols import ChannelConversationStore
from api.channel_execution.service import ChannelExecutionService
from api.db.db_models import get_async_db

router = APIRouter()


def _encode_sse(event: ExecutionEvent) -> str:
    payload = event.model_dump(mode="json", exclude_none=True)
    return "data:" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


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
    _workload: WorkloadIdentity = Depends(require_channel_workload),
    store: ChannelConversationStore = Depends(get_channel_conversation_store),
    db: AsyncSession = Depends(get_async_db),
) -> Response:
    """Reset only a server-resolved active binding conversation."""

    bundle = await SqlAlchemyChannelRepository(db).get_runtime_binding(binding_id)
    if bundle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="BINDING_NOT_FOUND")
    channel, binding, _secret = bundle
    if channel.status != 1 or not binding.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="BINDING_DISABLED")
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
