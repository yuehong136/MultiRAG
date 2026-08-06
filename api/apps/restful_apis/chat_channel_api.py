"""REST management API for MultiRAG chat channels.

Routes are mounted under ``/api/v1`` by ``api.apps.register_page``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from fastapi import APIRouter, Depends

from api.channel_control.dependencies import get_channel_control_service
from api.channel_control.schemas import (
    ChannelBindingUpsertRequest,
    ChannelCreateRequest,
    ChannelUpdateRequest,
    provider_manifests,
)
from api.channel_control.service import (
    ChannelAccessDenied,
    ChannelControlError,
    ChannelControlService,
    ChannelCredentialUnavailable,
    ChannelVerificationInconclusive,
    ChannelVerificationThrottled,
)
from api.utils.api_utils import Principal, async_current_user, get_json_result
from common.constants import RetCode

router = APIRouter()
LOGGER = logging.getLogger(__name__)
ResultT = TypeVar("ResultT")


async def _respond(operation: Callable[[], Awaitable[ResultT]]):
    """Run one control-plane operation and shape its failure for the UI.

    Failures carry a machine-readable ``error_code`` in ``data``. The service
    layer has always produced those codes; this boundary used to drop them and
    return ``data=False``, so the client saw four operationally distinct
    failures -- stale agent revision, credential missing, secret store down,
    not your channel -- as one indistinguishable rejection. ``retcode`` and
    ``retmsg`` are unchanged, and the client already surfaces ``data`` as
    ``APIError.details``, so nothing else has to move for this to be readable.
    """

    try:
        return get_json_result(data=await operation())
    except ChannelAccessDenied as error:
        return get_json_result(
            retcode=RetCode.AUTHENTICATION_ERROR,
            retmsg=error.safe_message,
            data={"error_code": error.error_code},
        )
    except ChannelCredentialUnavailable as error:
        return get_json_result(
            retcode=RetCode.CONNECTION_ERROR,
            retmsg=error.safe_message,
            data={"error_code": error.error_code},
        )
    except ChannelVerificationInconclusive as error:
        # Not an argument error: the credential may be perfectly good and the
        # probe simply could not reach the provider. Mapping this to the same
        # code as a rejection is what would make an admin re-enter a working
        # secret, which is the failure this endpoint exists to prevent.
        return get_json_result(
            retcode=RetCode.CONNECTION_ERROR,
            retmsg=error.safe_message,
            data={"error_code": error.error_code},
        )
    except ChannelVerificationThrottled as error:
        return get_json_result(
            retcode=RetCode.RESOURCE_EXHAUSTED,
            retmsg=error.safe_message,
            data={"error_code": error.error_code},
        )
    except ChannelControlError as error:
        return get_json_result(
            retcode=RetCode.ARGUMENT_ERROR,
            retmsg=error.safe_message,
            data={"error_code": error.error_code},
        )
    except Exception as error:
        LOGGER.error(
            "channel_control_event=request_failed error_type=%s",
            type(error).__name__,
        )
        # The catch-all needs a code of its own: it is the most likely failure
        # to reach an admin, and leaving it uncoded would be the one case with
        # no actionable text.
        return get_json_result(
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg="Channel operation failed.",
            data={"error_code": "CHANNEL_OPERATION_FAILED"},
        )


@router.get("/chat-channels/providers", summary="List supported chat channel providers")
async def list_channel_providers(user: Principal = Depends(async_current_user)):
    del user
    return get_json_result(data={"items": [manifest.model_dump(mode="json") for manifest in provider_manifests()]})


@router.get("/chat-channels", summary="List tenant chat channels")
async def list_chat_channels(
    service: ChannelControlService = Depends(get_channel_control_service),
    user: Principal = Depends(async_current_user),
):
    return await _respond(lambda: service.list_channels(user.id))


@router.post("/chat-channels", summary="Create a tenant chat channel")
async def create_chat_channel(
    request: ChannelCreateRequest,
    service: ChannelControlService = Depends(get_channel_control_service),
    user: Principal = Depends(async_current_user),
):
    return await _respond(lambda: service.create_channel(user.id, request))


@router.get("/chat-channels/{channel_id}", summary="Get a tenant chat channel")
async def get_chat_channel(
    channel_id: str,
    service: ChannelControlService = Depends(get_channel_control_service),
    user: Principal = Depends(async_current_user),
):
    return await _respond(lambda: service.get_channel(user.id, channel_id))


@router.patch("/chat-channels/{channel_id}", summary="Update a tenant chat channel")
async def update_chat_channel(
    channel_id: str,
    request: ChannelUpdateRequest,
    service: ChannelControlService = Depends(get_channel_control_service),
    user: Principal = Depends(async_current_user),
):
    return await _respond(lambda: service.update_channel(user.id, channel_id, request))


@router.delete("/chat-channels/{channel_id}", summary="Delete a tenant chat channel")
async def delete_chat_channel(
    channel_id: str,
    service: ChannelControlService = Depends(get_channel_control_service),
    user: Principal = Depends(async_current_user),
):
    return await _respond(lambda: service.delete_channel(user.id, channel_id))


@router.put("/chat-channels/{channel_id}/binding", summary="Create or replace a channel target binding")
async def upsert_chat_channel_binding(
    channel_id: str,
    request: ChannelBindingUpsertRequest,
    service: ChannelControlService = Depends(get_channel_control_service),
    user: Principal = Depends(async_current_user),
):
    return await _respond(lambda: service.upsert_binding(user.id, channel_id, request))


@router.post("/chat-channels/{channel_id}/enable", summary="Enable a channel binding")
async def enable_chat_channel(
    channel_id: str,
    service: ChannelControlService = Depends(get_channel_control_service),
    user: Principal = Depends(async_current_user),
):
    return await _respond(lambda: service.set_enabled(user.id, channel_id, enabled=True))


@router.post("/chat-channels/{channel_id}/disable", summary="Disable a channel binding")
async def disable_chat_channel(
    channel_id: str,
    service: ChannelControlService = Depends(get_channel_control_service),
    user: Principal = Depends(async_current_user),
):
    return await _respond(lambda: service.set_enabled(user.id, channel_id, enabled=False))


@router.post("/chat-channels/{channel_id}/verify", summary="Check a saved channel credential against its provider")
async def verify_chat_channel_credential(
    channel_id: str,
    service: ChannelControlService = Depends(get_channel_control_service),
    user: Principal = Depends(async_current_user),
):
    # No request body on purpose: the credential being checked is the stored
    # one. See ChannelControlService.verify_channel_credential.
    return await _respond(lambda: service.verify_channel_credential(user.id, channel_id))


@router.get("/chat-channels/{channel_id}/runtime", summary="Get sanitized channel runtime state")
async def get_chat_channel_runtime(
    channel_id: str,
    service: ChannelControlService = Depends(get_channel_control_service),
    user: Principal = Depends(async_current_user),
):
    return await _respond(lambda: service.get_runtime(user.id, channel_id))
