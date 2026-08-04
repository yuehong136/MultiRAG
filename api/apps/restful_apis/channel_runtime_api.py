"""Private control API consumed only by independent Channel runtimes."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status

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
    DesiredRuntimeList,
    RuntimeBindingConfig,
    RuntimeCredential,
    RuntimeReport,
)

router = APIRouter()


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
        return DesiredRuntimeList.model_validate({"items": await service.list_desired_runtimes()})
    except ChannelControlError as error:
        _raise_private_error(error)
        raise AssertionError("unreachable")


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
            app_id=runtime.credentials["app_id"],
            app_secret=runtime.credentials["app_secret"],
        ),
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
