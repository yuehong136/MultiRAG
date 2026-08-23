"""Generation-scoped private API for durable Channel interaction delivery."""

from __future__ import annotations

from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from api.channel_execution.dependencies import (
    get_interaction_delivery_lease_seconds,
    require_channel_workload,
    require_interaction_presentation_service,
)
from api.channel_execution.interaction_presentations import (
    CallbackReceiptResult,
    InteractionCallbackPayload,
    InteractionDelivery,
    InteractionPresentationError,
    InteractionPresentationErrorCode,
    InteractionPresentationService,
)
from api.channel_execution.models import WorkloadIdentity

router = APIRouter()

BindingId = Annotated[str, Path(min_length=1, max_length=32)]
DeliveryId = Annotated[str, Path(min_length=1, max_length=32)]
ActionId = Annotated[str, Path(min_length=1, max_length=32)]
Revision = Annotated[int, Path(gt=0)]
_ModelT = TypeVar("_ModelT", bound=BaseModel)
_SMALL_BODY_LIMIT = 4_096
_CALLBACK_BODY_LIMIT = 65_536


class ClaimInteractionDeliveryRequest(BaseModel):
    """Select one pending delivery without accepting authority-bearing fields."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    owner: str = Field(min_length=1, max_length=64, repr=False)
    action_id: str | None = Field(default=None, min_length=1, max_length=32, repr=False)
    revision: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_scope(self) -> ClaimInteractionDeliveryRequest:
        if not self.owner.strip() or self.owner != self.owner.strip():
            raise ValueError("delivery owner is invalid")
        if (self.action_id is None) != (self.revision is None):
            raise ValueError("action_id and revision must be supplied together")
        return self


class AcknowledgeInteractionDeliveryRequest(BaseModel):
    """Complete or retry only the exact delivery lease returned by claim."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    owner: str = Field(min_length=1, max_length=64, repr=False)
    delivery_token: SecretStr = Field(min_length=16, max_length=255, repr=False)
    success: bool
    safe_error_code: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{0,63}$",
    )

    @model_validator(mode="after")
    def validate_owner(self) -> AcknowledgeInteractionDeliveryRequest:
        if not self.owner.strip() or self.owner != self.owner.strip():
            raise ValueError("delivery owner is invalid")
        if self.success and self.safe_error_code is not None:
            raise ValueError("successful delivery cannot carry an error code")
        return self


async def _parse_private_body(
    request: Request,
    *,
    model: type[_ModelT],
    max_bytes: int,
) -> _ModelT:
    """Validate a bounded body without reflecting untrusted input in a 422."""

    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="INTERACTION_REQUEST_CONTENT_TYPE_INVALID",
        )
    raw_content_length = request.headers.get("content-length")
    if raw_content_length is not None:
        try:
            content_length = int(raw_content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="INTERACTION_REQUEST_INVALID",
            ) from exc
        if content_length < 1 or content_length > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="INTERACTION_REQUEST_INVALID",
            )
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="INTERACTION_REQUEST_INVALID",
            )
        body.extend(chunk)
    if not body:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="INTERACTION_REQUEST_INVALID",
        )
    try:
        return model.model_validate_json(bytes(body))
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="INTERACTION_REQUEST_INVALID",
        ) from exc


async def _parse_claim_request(request: Request) -> ClaimInteractionDeliveryRequest:
    return await _parse_private_body(
        request,
        model=ClaimInteractionDeliveryRequest,
        max_bytes=_SMALL_BODY_LIMIT,
    )


async def _parse_ack_request(request: Request) -> AcknowledgeInteractionDeliveryRequest:
    return await _parse_private_body(
        request,
        model=AcknowledgeInteractionDeliveryRequest,
        max_bytes=_SMALL_BODY_LIMIT,
    )


async def _parse_callback_payload(request: Request) -> InteractionCallbackPayload:
    return await _parse_private_body(
        request,
        model=InteractionCallbackPayload,
        max_bytes=_CALLBACK_BODY_LIMIT,
    )


def _require_binding_generation(
    *,
    binding_id: str,
    workload: WorkloadIdentity,
) -> int:
    generation = workload.binding_generation
    if workload.binding_id != binding_id or generation is None or generation < 1:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized channel runtime.",
        )
    return generation


def _presentation_http_error(error: InteractionPresentationError) -> HTTPException:
    if error.code in {
        InteractionPresentationErrorCode.NOT_FOUND,
        InteractionPresentationErrorCode.BINDING_MISMATCH,
    }:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=InteractionPresentationErrorCode.NOT_FOUND.value,
        )
    if error.code is InteractionPresentationErrorCode.PAYLOAD_INVALID:
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error.code.value)
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error.code.value)


@router.post(
    "/internal/channel-bindings/{binding_id}/interaction-deliveries/claim",
    response_model=InteractionDelivery,
    responses={204: {"description": "No pending delivery"}},
    include_in_schema=False,
)
async def claim_interaction_delivery(
    response: Response,
    binding_id: BindingId,
    workload: WorkloadIdentity = Depends(require_channel_workload),
    request: ClaimInteractionDeliveryRequest = Depends(_parse_claim_request),
    service: InteractionPresentationService = Depends(require_interaction_presentation_service),
    lease_seconds: int = Depends(get_interaction_delivery_lease_seconds),
) -> InteractionDelivery | Response:
    """Lease one safe projection for this authenticated binding generation."""

    generation = _require_binding_generation(binding_id=binding_id, workload=workload)
    try:
        delivery = await service.claim_delivery(
            binding_id=binding_id,
            binding_generation=generation,
            owner=request.owner,
            lease_seconds=lease_seconds,
            action_id=request.action_id,
            revision=request.revision,
        )
    except InteractionPresentationError as exc:
        raise _presentation_http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="INTERACTION_DELIVERY_REQUEST_INVALID",
        ) from exc
    if delivery is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    response.headers["Cache-Control"] = "private, no-store"
    return delivery


@router.post(
    "/internal/channel-bindings/{binding_id}/interaction-deliveries/{delivery_id}/ack",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
async def acknowledge_interaction_delivery(
    binding_id: BindingId,
    delivery_id: DeliveryId,
    workload: WorkloadIdentity = Depends(require_channel_workload),
    request: AcknowledgeInteractionDeliveryRequest = Depends(_parse_ack_request),
    service: InteractionPresentationService = Depends(require_interaction_presentation_service),
) -> Response:
    """Acknowledge a one-time delivery lease without executing any MCP work."""

    generation = _require_binding_generation(binding_id=binding_id, workload=workload)
    try:
        await service.acknowledge_delivery(
            binding_id=binding_id,
            binding_generation=generation,
            delivery_id=delivery_id,
            owner=request.owner,
            delivery_token=request.delivery_token.get_secret_value(),
            success=request.success,
            safe_error_code=request.safe_error_code,
        )
    except InteractionPresentationError as exc:
        raise _presentation_http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="INTERACTION_DELIVERY_REQUEST_INVALID",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/internal/channel-bindings/{binding_id}/interactions/{action_id}/revisions/{revision}/callbacks",
    response_model=CallbackReceiptResult,
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
)
async def receive_interaction_callback(
    response: Response,
    binding_id: BindingId,
    action_id: ActionId,
    revision: Revision,
    workload: WorkloadIdentity = Depends(require_channel_workload),
    payload: InteractionCallbackPayload = Depends(_parse_callback_payload),
    service: InteractionPresentationService = Depends(require_interaction_presentation_service),
) -> CallbackReceiptResult:
    """Durably receipt a bounded callback, then ACK without executing MCP."""

    generation = _require_binding_generation(binding_id=binding_id, workload=workload)
    try:
        result = await service.receive_callback(
            binding_id=binding_id,
            binding_generation=generation,
            action_id=action_id,
            revision=revision,
            payload=payload,
        )
    except InteractionPresentationError as exc:
        raise _presentation_http_error(exc) from exc
    response.headers["Cache-Control"] = "private, no-store"
    return result


__all__ = [
    "AcknowledgeInteractionDeliveryRequest",
    "ClaimInteractionDeliveryRequest",
    "router",
]
