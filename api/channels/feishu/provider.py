"""Feishu worker descriptor: SDK wiring, account rules and tuning mapping.

Everything Feishu-specific that the generic runner used to hardcode lives here,
so `api/channels/worker.py` imports no transport and adding a provider means
adding a sibling directory rather than editing the runner.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from api.channel_runtime.schemas import RuntimeCredential
from api.channels.feishu.channel import FeishuAccount, FeishuChannel
from api.channels.provider import ChannelWorkerError, ManagedChannelPlan, WorkerTuning
from common.app_config import AppConfig

_PROVIDER_NAME = "feishu"
# Feishu (mainland) and Lark (international) are separate API hosts, so the value
# selects an endpoint rather than a display preference.
_ACCOUNT_DOMAINS = frozenset({"feishu", "lark"})


def _resolve_domain(public_config: Mapping[str, object]) -> str:
    """Resolve the canonical and upstream-compatible domain shapes, fail closed.

    ``config.domain`` is canonical; ``config.credential.domain`` is accepted for
    upstream request compatibility and only consulted when the root field is
    absent. A missing or unknown value is an error: silently defaulting would
    point an international Lark account at the mainland host.
    """

    domain = public_config.get("domain")
    if domain is None:
        credential = public_config.get("credential")
        if isinstance(credential, Mapping):
            domain = credential.get("domain")
    if domain not in _ACCOUNT_DOMAINS:
        raise ChannelWorkerError("CHANNEL_RUNTIME_CONFIG_INVALID")
    return str(domain)


def _resolve_allowed_open_ids(public_config: Mapping[str, object]) -> frozenset[str]:
    """Read the optional sender allowlist, rejecting anything malformed."""

    raw = public_config.get("allowed_open_ids", [])
    if not isinstance(raw, list) or any(not isinstance(open_id, str) or not open_id.strip() for open_id in raw):
        raise ChannelWorkerError("CHANNEL_RUNTIME_CONFIG_INVALID")
    return frozenset(raw)


class FeishuWorkerProvider:
    """Build one Feishu transport out of server-owned binding state."""

    @property
    def name(self) -> str:
        return _PROVIDER_NAME

    def tuning(self, app_config: AppConfig) -> WorkerTuning:
        section = app_config.channels.feishu
        return WorkerTuning(
            queue_size=section.queue_size,
            worker_concurrency=section.worker_concurrency,
            dedupe_ttl_seconds=section.dedupe_ttl_seconds,
            session_ttl_seconds=section.session_ttl_seconds,
            leader_ttl_seconds=section.leader_ttl_seconds,
            leader_renew_seconds=section.leader_renew_seconds,
            max_question_chars=section.max_question_chars,
            max_answer_chars=section.max_answer_chars,
            # Pydantic keeps this a PositiveInt while the HTTP boundary accepts
            # seconds as a float; convert once so Beartype sees the declared type.
            total_timeout_seconds=float(section.total_timeout_seconds),
        )

    def build_managed(
        self,
        *,
        credential: RuntimeCredential,
        public_config: Mapping[str, object],
    ) -> ManagedChannelPlan:
        return ManagedChannelPlan(
            channel=FeishuChannel(
                FeishuAccount(
                    # The transport only logs a hashed account id.
                    account_id=hashlib.sha256(credential.app_id.encode("utf-8")).hexdigest()[:16],
                    app_id=credential.app_id,
                    app_secret=credential.app_secret,
                    domain=_resolve_domain(public_config),
                )
            ),
            account_id=credential.app_id,
            allowed_sender_ids=_resolve_allowed_open_ids(public_config),
        )


WORKER_PROVIDER = FeishuWorkerProvider()
