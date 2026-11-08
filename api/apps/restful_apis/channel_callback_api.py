"""Shared API listener for signed callbacks; no binding owns a TCP port."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from redis.asyncio import Redis
from redis.exceptions import RedisError

from api.channel_control.callback_inbox import CallbackInbox
from api.channel_control.callback_validation import CallbackRejected, line_messages, wecom_decrypt, wecom_messages
from api.channel_control.dependencies import get_runtime_control_service
from api.channel_control.service import ChannelControlError, ChannelControlService, RuntimeBindingSpec
from api.channel_execution.dependencies import get_channel_execution_redis, require_channel_workload
from api.channel_execution.models import WorkloadIdentity
from api.channel_runtime.callbacks import CallbackAck, CallbackBatch

router = APIRouter()
_PROVIDERS = frozenset({"line", "wecom"})


async def _binding(service: ChannelControlService, binding_id: str, *, generation: int | None = None) -> RuntimeBindingSpec:
    try:
        runtime = await service.load_runtime_binding(binding_id)
    except ChannelControlError:
        raise HTTPException(404, "CHANNEL_CALLBACK_UNAVAILABLE") from None
    if runtime.provider not in _PROVIDERS or (generation is not None and runtime.generation != generation):
        raise HTTPException(404, "CHANNEL_CALLBACK_UNAVAILABLE")
    return runtime


async def _body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 1_048_576:
            raise HTTPException(413, "CHANNEL_CALLBACK_TOO_LARGE")
        body.extend(chunk)
    return bytes(body)


@router.api_route("/chat-channels/callbacks/{provider}/{binding_id}", methods=["GET", "POST"], include_in_schema=False)
async def receive_channel_callback(
    request: Request,
    provider: str,
    binding_id: str = Path(min_length=1, max_length=32),
    service: ChannelControlService = Depends(get_runtime_control_service),
    redis: Redis = Depends(get_channel_execution_redis),
) -> Response:
    if provider not in _PROVIDERS or (request.method == "GET" and provider != "wecom"):
        raise HTTPException(404, "CHANNEL_CALLBACK_UNAVAILABLE")
    runtime = await _binding(service, binding_id)
    if runtime.provider != provider:
        raise HTTPException(404, "CHANNEL_CALLBACK_UNAVAILABLE")
    try:
        resolved = await service.resolve_runtime_binding(binding_id, expected_generation=runtime.generation)
    except ChannelControlError:
        raise HTTPException(503, "CHANNEL_CALLBACK_UNAVAILABLE") from None
    try:
        if request.method == "GET":
            echo = wecom_decrypt(request.query_params.get("echostr", ""), request.query_params, resolved.credentials)
            return Response(echo, media_type="text/plain")
        body = await _body(request)
        messages = line_messages(body, request.headers.get("x-line-signature", ""), resolved.credentials) if provider == "line" else wecom_messages(body, request.query_params, resolved.credentials)
    except CallbackRejected as error:
        raise HTTPException(403, str(error)) from None
    except KeyError:
        raise HTTPException(503, "CHANNEL_CALLBACK_CREDENTIAL_INVALID") from None
    try:
        admitted = await CallbackInbox(redis, runtime.tenant_id, binding_id, runtime.generation).admit(messages)
    except RedisError:
        raise HTTPException(503, "CHANNEL_CALLBACK_INBOX_UNAVAILABLE") from None
    if not admitted:
        raise HTTPException(503, "CHANNEL_CALLBACK_INBOX_FULL")
    return Response("ok" if provider == "line" else "", media_type="text/plain")


async def _worker_binding(service: ChannelControlService, workload: WorkloadIdentity, binding_id: str) -> RuntimeBindingSpec:
    if workload.binding_id != binding_id or workload.binding_generation is None:
        raise HTTPException(401, "Unauthorized channel runtime.")
    return await _binding(service, binding_id, generation=workload.binding_generation)


@router.get("/internal/channel-bindings/{binding_id}/callback-inbox", response_model=CallbackBatch, include_in_schema=False)
async def peek_channel_callback(
    binding_id: str = Path(min_length=1, max_length=32),
    workload: WorkloadIdentity = Depends(require_channel_workload),
    service: ChannelControlService = Depends(get_runtime_control_service),
    redis: Redis = Depends(get_channel_execution_redis),
) -> CallbackBatch:
    runtime = await _worker_binding(service, workload, binding_id)
    try:
        return await CallbackInbox(redis, runtime.tenant_id, binding_id, runtime.generation).peek()
    except RedisError:
        raise HTTPException(503, "CHANNEL_CALLBACK_INBOX_UNAVAILABLE") from None


@router.post("/internal/channel-bindings/{binding_id}/callback-inbox/ack", status_code=204, include_in_schema=False)
async def ack_channel_callback(
    ack: CallbackAck,
    binding_id: str = Path(min_length=1, max_length=32),
    workload: WorkloadIdentity = Depends(require_channel_workload),
    service: ChannelControlService = Depends(get_runtime_control_service),
    redis: Redis = Depends(get_channel_execution_redis),
) -> Response:
    runtime = await _worker_binding(service, workload, binding_id)
    try:
        accepted = await CallbackInbox(redis, runtime.tenant_id, binding_id, runtime.generation).ack(ack.receipt)
    except RedisError:
        raise HTTPException(503, "CHANNEL_CALLBACK_INBOX_UNAVAILABLE") from None
    if not accepted:
        raise HTTPException(409, "CHANNEL_CALLBACK_RECEIPT_STALE")
    return Response(status_code=204)
