"""Private generation-scoped API for durable Channel identity events."""

from __future__ import annotations

import json
import re
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import ValidationError

from api.channel_execution.dependencies import (
    get_channel_directory_event_service,
    require_channel_workload,
)
from api.channel_execution.models import WorkloadIdentity
from api.channels.identity_events import ChannelIdentityEvent
from api.identity.directory_events import DirectoryEventError, DirectoryEventErrorCode
from api.identity_adapters.channel_directory_events import ChannelDirectoryEventService

router = APIRouter()
_MAX_IDENTITY_EVENT_BODY_BYTES = 4096
_AUTHORITY_UNAVAILABLE = "IDENTITY_DIRECTORY_AUTHORITY_UNAVAILABLE"
_CONTENT_TYPE_UNSUPPORTED = "IDENTITY_DIRECTORY_CONTENT_TYPE_UNSUPPORTED"
_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
_BINDING_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _raise_invalid_event() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=DirectoryEventErrorCode.INVALID.value,
    )


async def _read_bounded_payload(request: Request) -> dict[str, object]:
    if request.headers.get("Content-Type") != "application/json":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=_CONTENT_TYPE_UNSUPPORTED,
        )
    raw_content_length = request.headers.get("Content-Length")
    if raw_content_length is not None:
        try:
            content_length = int(raw_content_length)
        except ValueError:
            _raise_invalid_event()
        if content_length <= 0 or content_length > _MAX_IDENTITY_EVENT_BODY_BYTES:
            _raise_invalid_event()
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_IDENTITY_EVENT_BODY_BYTES:
            _raise_invalid_event()
        body.extend(chunk)
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _raise_invalid_event()
    if not isinstance(decoded, dict) or any(type(key) is not str for key in decoded):
        _raise_invalid_event()
    return decoded


def _raise_directory_event_error(error: DirectoryEventError) -> None:
    if error.code in {
        DirectoryEventErrorCode.BINDING_NOT_FOUND,
        DirectoryEventErrorCode.BINDING_DISABLED,
        DirectoryEventErrorCode.AUTHORITY_INVALID,
    }:
        status_code = status.HTTP_404_NOT_FOUND
        detail = _AUTHORITY_UNAVAILABLE
    elif error.code in {
        DirectoryEventErrorCode.RECEIPT_PROCESSING,
        DirectoryEventErrorCode.REPOSITORY_UNAVAILABLE,
    }:
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        detail = error.code.value
    elif error.code is DirectoryEventErrorCode.INVALID:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        detail = error.code.value
    else:
        status_code = status.HTTP_409_CONFLICT
        detail = error.code.value
    raise HTTPException(status_code=status_code, detail=detail) from error


@router.post(
    "/internal/channel-bindings/{binding_id}/identity-events",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
async def receive_channel_identity_event(
    request: Request,
    binding_id: str,
    workload: WorkloadIdentity = Depends(require_channel_workload),
    service: ChannelDirectoryEventService = Depends(
        get_channel_directory_event_service,
    ),
) -> Response:
    """Commit one normalized event without trusting payload authority fields."""

    if not _BINDING_ID_PATTERN.fullmatch(binding_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_AUTHORITY_UNAVAILABLE,
        )
    if workload.binding_id != binding_id or workload.binding_generation is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized channel runtime.",
        )
    payload = await _read_bounded_payload(request)
    idempotency_key = request.headers.get("Idempotency-Key")
    if type(idempotency_key) is not str or not _EVENT_ID_PATTERN.fullmatch(idempotency_key):
        _raise_invalid_event()
    try:
        event = ChannelIdentityEvent.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=DirectoryEventErrorCode.INVALID.value,
        ) from exc
    if idempotency_key != event.event_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=DirectoryEventErrorCode.REPLAY_CONFLICT.value,
        )
    try:
        await service.receive(
            binding_id=binding_id,
            binding_generation=workload.binding_generation,
            event=event,
        )
    except DirectoryEventError as error:
        _raise_directory_event_error(error)
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"Cache-Control": "private, no-store"},
    )
