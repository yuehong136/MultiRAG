"""Feishu composition for safe interaction delivery projections."""

from __future__ import annotations

from api.channels.feishu.interaction_renderer import (
    FeishuInteractionCardTransport,
    FeishuInteractionRenderer,
)
from api.channels.interaction_models import (
    ChannelInteractionFormProjection,
    ChannelInteractionTerminalProjection,
    ClaimedInteractionDelivery,
)

_RETRY_PREFIX = "上次提交未通过校验，请检查后重试。"


class FeishuInteractionPresenter:
    """Replace one finished reply card from an API-approved projection."""

    def __init__(self, transport: FeishuInteractionCardTransport) -> None:
        self._renderer = FeishuInteractionRenderer(transport)

    async def present(self, delivery: ClaimedInteractionDelivery) -> None:
        projection = delivery.projection
        if delivery.kind == "form":
            if not isinstance(projection, ChannelInteractionFormProjection) or delivery.action_nonce is None:
                raise ValueError("form interaction delivery is invalid")
            if delivery.safe_error_code is not None:
                message = f"{_RETRY_PREFIX} {projection.message}"
                projection = projection.model_copy(
                    update={"message": message[:240]},
                )
            await self._renderer.present(
                reply_message_id=delivery.presentation_ref,
                action_id=delivery.action_id,
                action_nonce=delivery.action_nonce.get_secret_value(),
                revision=delivery.revision,
                expires_at=delivery.expires_at,
                projection=projection,
            )
            return
        if not isinstance(projection, ChannelInteractionTerminalProjection):
            raise ValueError("terminal interaction delivery is invalid")
        await self._renderer.present_terminal(
            reply_message_id=delivery.presentation_ref,
            projection=projection,
        )


__all__ = ["FeishuInteractionPresenter"]
