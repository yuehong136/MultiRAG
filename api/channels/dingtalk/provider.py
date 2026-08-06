"""DingTalk worker descriptor: transport wiring, account rules and tuning.

The sibling of `api/channels/feishu/provider.py`, and the reason the runner
imports no transport: adding a provider is adding a directory like this one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from api.channel_runtime.schemas import RuntimeCredential
from api.channels.dingtalk.channel import DingTalkAccount, DingTalkChannel
from api.channels.provider import ChannelWorkerError, ManagedChannelPlan, WorkerTuning
from common.app_config import AppConfig

_PROVIDER_NAME = "dingtalk"


def _resolve_allowed_user_ids(public_config: Mapping[str, object]) -> frozenset[str]:
    """Read the optional sender allowlist, rejecting anything malformed."""

    raw = public_config.get("allowed_user_ids", [])
    if not isinstance(raw, list) or any(not isinstance(user_id, str) or not user_id.strip() for user_id in raw):
        raise ChannelWorkerError("CHANNEL_RUNTIME_CONFIG_INVALID")
    return frozenset(raw)


class DingTalkWorkerProvider:
    """Build one DingTalk transport out of server-owned binding state."""

    @property
    def name(self) -> str:
        return _PROVIDER_NAME

    def tuning(self, app_config: AppConfig) -> WorkerTuning:
        section = app_config.channels.dingtalk
        return WorkerTuning(
            queue_size=section.queue_size,
            worker_concurrency=section.worker_concurrency,
            dedupe_ttl_seconds=section.dedupe_ttl_seconds,
            session_ttl_seconds=section.session_ttl_seconds,
            leader_ttl_seconds=section.leader_ttl_seconds,
            leader_renew_seconds=section.leader_renew_seconds,
            max_question_chars=section.max_question_chars,
            max_answer_chars=section.max_answer_chars,
            total_timeout_seconds=float(section.total_timeout_seconds),
        )

    def build_managed(
        self,
        *,
        credential: RuntimeCredential,
        public_config: Mapping[str, object],
    ) -> ManagedChannelPlan:
        # Only the generic map: this provider was written after CHN-P8, so it
        # has no legacy pair to fall back to and must not invent one -- the
        # legacy fields are Feishu's names and are being deleted in CHN-P11.
        client_id = credential.value("client_id")
        client_secret = credential.value("client_secret")
        if not client_id or not client_secret:
            raise ChannelWorkerError("CHANNEL_RUNTIME_CONFIG_INVALID")

        return ManagedChannelPlan(
            channel=DingTalkChannel(
                DingTalkAccount(
                    # The transport only logs a hashed account id.
                    account_id=hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16],
                    client_id=client_id,
                    client_secret=client_secret,
                )
            ),
            account_id=client_id,
            allowed_sender_ids=_resolve_allowed_user_ids(public_config),
        )


WORKER_PROVIDER = DingTalkWorkerProvider()
