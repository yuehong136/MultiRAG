"""Provider seam that keeps the generic Channel worker free of transport imports.

The worker owns the bounded queue, dedupe, conversation ordering, leader lease
and shutdown; a provider owns only its SDK, its credential shape and its own
tuning section. Adding a transport is one entry in ``_PROVIDER_MODULES`` plus one
``<provider>/provider.py``.

Providers are imported lazily by name rather than eagerly from a bootstrap
module: ``lark-oapi`` installs a process-global event loop, so importing every
transport into every runner would couple unrelated SDKs into a process that
drives exactly one account.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from api.channel_runtime.schemas import RuntimeCredential
from api.channels.core.base import Channel
from common.app_config import AppConfig

_PROVIDER_MODULES: dict[str, str] = {
    "feishu": "api.channels.feishu.provider",
}


class ChannelWorkerError(RuntimeError):
    """A classified, non-sensitive worker lifecycle failure."""


class UnsupportedChannelProvider(ChannelWorkerError):
    """Raised for a provider name that has no registered worker descriptor."""


@dataclass(frozen=True, slots=True)
class WorkerTuning:
    """The only tuning the transport-agnostic worker pipeline needs.

    Each provider maps its own config section onto this, which also pins the
    int-to-float conversion the HTTP boundary expects in one place instead of
    leaving casts scattered through the runner.
    """

    queue_size: int
    worker_concurrency: int
    dedupe_ttl_seconds: int
    session_ttl_seconds: int
    leader_ttl_seconds: int
    leader_renew_seconds: int
    max_question_chars: int
    max_answer_chars: int
    total_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ManagedChannelPlan:
    """Everything a provider resolves out of one server-owned binding."""

    channel: Channel
    # Raw provider account identifier. The state store namespaces dedupe,
    # session and leader-lease keys by its hash, and that is what keeps two
    # accounts of the same provider from contending for one lease.
    account_id: str
    allowed_sender_ids: frozenset[str]


@runtime_checkable
class WorkerProvider(Protocol):
    """One transport's worker-facing descriptor."""

    @property
    def name(self) -> str: ...

    def tuning(self, app_config: AppConfig) -> WorkerTuning: ...

    def build_managed(
        self,
        *,
        credential: RuntimeCredential,
        public_config: Mapping[str, object],
    ) -> ManagedChannelPlan: ...


def supported_provider_names() -> tuple[str, ...]:
    """Provider names the runner and supervisor accept, in stable order."""

    return tuple(sorted(_PROVIDER_MODULES))


def worker_provider(name: str) -> WorkerProvider:
    """Import and return one provider descriptor, failing closed on unknowns."""

    module_path = _PROVIDER_MODULES.get(name)
    if module_path is None:
        raise UnsupportedChannelProvider("CHANNEL_NOT_SUPPORTED")
    provider = getattr(importlib.import_module(module_path), "WORKER_PROVIDER", None)
    if not isinstance(provider, WorkerProvider) or provider.name != name:
        raise UnsupportedChannelProvider("CHANNEL_NOT_SUPPORTED")
    return provider
