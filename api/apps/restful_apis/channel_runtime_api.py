"""Private control API consumed only by independent Channel runtimes."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import ValidationError

from api.channel_control.dependencies import get_runtime_control_service
from api.channel_control.service import (
    ChannelAccessDenied,
    ChannelControlError,
    ChannelControlService,
    ChannelCredentialUnavailable,
)
from api.channel_execution.dependencies import require_channel_workload
from api.channel_execution.models import WorkloadIdentity
from api.channel_runtime.schemas import (
    DesiredRuntime,
    DesiredRuntimeList,
    RuntimeBindingConfig,
    RuntimeCredential,
    RuntimeReport,
)

router = APIRouter()
LOGGER = logging.getLogger(__name__)


def _short_hash(value: str) -> str:
    """Log-safe binding identifier, matching what the supervisor logs.

    Defined locally rather than imported from ``api.channels``: this module
    runs inside the API process, and importing the transport package would
    pull a provider SDK (and its process-global event loop) in with it.
    """

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _raise_private_error(error: ChannelControlError) -> None:
    if isinstance(error, ChannelAccessDenied):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error.error_code) from error
    if isinstance(error, ChannelCredentialUnavailable):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=error.error_code) from error
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error.error_code) from error


@router.get(
    "/internal/channel-runtimes/desired",
    response_model=DesiredRuntimeList,
    summary="List desired Channel runtimes",
)
async def list_desired_channel_runtimes(
    _workload: WorkloadIdentity = Depends(require_channel_workload),
    service: ChannelControlService = Depends(get_runtime_control_service),
) -> DesiredRuntimeList:
    try:
        rows = await service.list_desired_runtimes()
    except ChannelControlError as error:
        _raise_private_error(error)
        raise AssertionError("unreachable")

    # Validate row by row and drop what will not parse. Validating the list as
    # a whole meant one unrecognised row raised out of this route -- uncaught,
    # since ValidationError is not a ChannelControlError -- and the supervisor
    # reads that HTTP 500 as "skip this entire reconcile tick". A single bad
    # row therefore stopped *every* binding from being started or reaped, not
    # just its own. Fail isolated, not fail closed.
    items: list[DesiredRuntime] = []
    for row in rows:
        try:
            items.append(DesiredRuntime.model_validate(row))
        except ValidationError:
            LOGGER.error(
                "channel_runtime_event=desired_row_rejected error_code=CHANNEL_DESIRED_ROW_INVALID binding_id_hash=%s",
                _short_hash(str(row.get("binding_id", ""))),
            )
    return DesiredRuntimeList(items=items)


@router.get(
    "/internal/channel-bindings/{binding_id}/runtime-config",
    response_model=RuntimeBindingConfig,
    include_in_schema=False,
)
async def get_channel_runtime_config(
    binding_id: str = Path(min_length=1, max_length=32),
    workload: WorkloadIdentity = Depends(require_channel_workload),
    service: ChannelControlService = Depends(get_runtime_control_service),
) -> RuntimeBindingConfig:
    if workload.binding_id != binding_id or workload.binding_generation is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized channel runtime.")
    try:
        runtime = await service.resolve_runtime_binding(
            binding_id,
            expected_generation=workload.binding_generation,
        )
    except ChannelControlError as error:
        _raise_private_error(error)
        raise AssertionError("unreachable")
    return RuntimeBindingConfig(
        binding_id=runtime.binding_id,
        provider=runtime.provider,
        generation=runtime.generation,
        public_config=runtime.public_config,
        credential=RuntimeCredential(
            # The whole credential, keyed by the leaf names the provider spec
            # declares -- which is what lets a second provider exist without
            # this route naming any of its fields. The Feishu-shaped legacy
            # pair that used to ride alongside is gone as of CHN-P11.
            fields=runtime.credentials,
        ),
        # Emit half of CHN-O2 -> CHN-O3. Until now this reached the database and
        # stopped there, so the "private chats only" toggle was decoration.
        policy=runtime.policy,
    )


@router.put(
    "/internal/channel-bindings/{binding_id}/runtime-status",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
async def report_channel_runtime_status(
    report: RuntimeReport,
    binding_id: str = Path(min_length=1, max_length=32),
    workload: WorkloadIdentity = Depends(require_channel_workload),
    service: ChannelControlService = Depends(get_runtime_control_service),
) -> Response:
    if workload.binding_id != binding_id or workload.binding_generation is None or workload.binding_generation != report.observed_generation:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized channel runtime.")
    try:
        await service.report_runtime(
            binding_id=binding_id,
            observed_generation=report.observed_generation,
            state=report.state,
            runner_id=report.runner_id,
            heartbeat_at=datetime.now(UTC),
            connected_at=report.connected_at,
            last_error_code=report.last_error_code,
        )
    except ChannelControlError as error:
        _raise_private_error(error)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
