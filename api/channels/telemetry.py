"""Provider-neutral, process-local telemetry for Channel lifecycles.

The hot-path contract is deliberately small: every method is synchronous and
records only closed labels plus numeric values.  The default implementation is
a no-op so reusable Channel components never acquire an exporter dependency.
Production composition roots explicitly inject the bounded process recorder
below; exporting or aggregating its snapshots across processes is future work.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable


class ChannelProvider(StrEnum):
    """Closed provider label; unknown future providers collapse to ``other``."""

    FEISHU = "feishu"
    DINGTALK = "dingtalk"
    INTERNAL = "internal"
    OTHER = "other"


class ChannelOperation(StrEnum):
    """Closed operation label shared by ingress and background work."""

    MESSAGE = "message"
    REGENERATE = "regenerate"
    MAINTENANCE = "maintenance"
    OTHER = "other"


class ChannelResult(StrEnum):
    """Closed result label used across Channel counters and histograms."""

    OK = "ok"
    FAILED = "failed"
    REJECTED = "rejected"
    DROPPED = "dropped"
    DUPLICATE = "duplicate"
    CANCELLED = "cancelled"
    AWAITING_INPUT = "awaiting_input"
    FALLBACK = "fallback"
    DISABLED = "disabled"


class ChannelReason(StrEnum):
    """Closed, identity-free reasons suitable for bounded metric labels."""

    NONE = "none"
    PRIVATE_CHAT_REQUIRED = "private_chat_required"
    USER_SENDER_REQUIRED = "user_sender_required"
    MESSAGE_IDENTITY_MISSING = "message_identity_missing"
    POLICY_REJECTED = "policy_rejected"
    WORKER_STOPPING = "worker_stopping"
    FOLLOWUP_QUEUE_FULL = "followup_queue_full"
    GLOBAL_QUEUE_FULL = "global_queue_full"
    QUEUE_ABANDONED = "queue_abandoned"
    HANDLER_FAILURE = "handler_failure"
    STATE_FAILURE = "state_failure"
    DUPLICATE = "duplicate"
    EXECUTION_FAILURE = "execution_failure"
    DELIVERY_FAILURE = "delivery_failure"
    SHUTDOWN = "shutdown"
    SHUTDOWN_TIMEOUT = "shutdown_timeout"
    CARD_CREATE_FAILURE = "card_create_failure"
    CARD_UPDATE_FAILURE = "card_update_failure"
    CARD_FINISH_FAILURE = "card_finish_failure"
    CARD_CONTROLS_FAILURE = "card_controls_failure"
    CARD_FAILURE_RENDER_FAILURE = "card_failure_render_failure"
    CARD_CANCEL_RENDER_FAILURE = "card_cancel_render_failure"
    TYPING_FAILURE = "typing_failure"
    POST_FALLBACK_FAILURE = "post_fallback_failure"
    TEXT_FALLBACK_FAILURE = "text_fallback_failure"
    CANDIDATE_GC_FAILURE = "candidate_gc_failure"


class ChannelStage(StrEnum):
    """Closed lifecycle stage label."""

    INGRESS = "ingress"
    QUEUE = "queue"
    EXECUTION = "execution"
    DELIVERY = "delivery"
    CARD_CREATE = "card_create"
    CARD_UPDATE = "card_update"
    CARD_FINISH = "card_finish"
    CARD_CONTROLS = "card_controls"
    CARD_FAILURE = "card_failure"
    CARD_CANCEL = "card_cancel"
    TYPING = "typing"
    FALLBACK_POST = "fallback_post"
    FALLBACK_TEXT = "fallback_text"
    SHUTDOWN = "shutdown"
    CANDIDATE_GC = "candidate_gc"
    CANDIDATE_GC_EXPLICIT_CANVAS = "candidate_gc_explicit_canvas"
    CANDIDATE_GC_LEGACY_CANVAS = "candidate_gc_legacy_canvas"
    CANDIDATE_GC_LEGACY_DIALOG = "candidate_gc_legacy_dialog"


class ChannelMetric(StrEnum):
    """The complete first-wave CHN-O9 metric inventory."""

    MESSAGES_TOTAL = "messages_total"
    QUEUE_DEPTH = "queue_depth"
    QUEUE_WAIT_SECONDS = "queue_wait_seconds"
    QUEUE_ABANDONED_TOTAL = "queue_abandoned_total"
    FIRST_CARD_SECONDS = "first_card_seconds"
    FIRST_CONTENT_SECONDS = "first_content_seconds"
    EXECUTION_DURATION_SECONDS = "execution_duration_seconds"
    DELIVERY_TOTAL = "delivery_total"
    SHUTDOWN_TOTAL = "shutdown_total"
    SHUTDOWN_REPLIES_TOTAL = "shutdown_replies_total"
    CANDIDATE_GC_CYCLES_TOTAL = "candidate_gc_cycles_total"
    CANDIDATE_GC_DURATION_SECONDS = "candidate_gc_duration_seconds"
    CANDIDATE_GC_DELETED_TOTAL = "candidate_gc_deleted_total"
    CANDIDATE_GC_BATCHES = "candidate_gc_batches"
    CANDIDATE_GC_REMAINING_TOTAL = "candidate_gc_remaining_total"


@dataclass(frozen=True, slots=True)
class ChannelTelemetryLabels:
    """Only values allowed to identify a telemetry series."""

    provider: ChannelProvider
    operation: ChannelOperation
    result: ChannelResult
    reason: ChannelReason
    stage: ChannelStage


@dataclass(frozen=True, slots=True)
class ChannelTelemetryEvent:
    """One bounded recorder observation exposed for tests and diagnostics."""

    metric: ChannelMetric
    labels: ChannelTelemetryLabels
    value: float


@dataclass(frozen=True, slots=True)
class ChannelTelemetrySnapshot:
    """Immutable point-in-time view of one process recorder."""

    counters: Mapping[tuple[ChannelMetric, ChannelTelemetryLabels], float]
    histograms: Mapping[tuple[ChannelMetric, ChannelTelemetryLabels], tuple[float, ...]]
    recent_events: tuple[ChannelTelemetryEvent, ...]
    dropped_series: int


@dataclass(frozen=True, slots=True)
class ChannelMessageOutcome:
    """Final disposition returned by a bridge to the worker queue owner."""

    result: ChannelResult
    reason: ChannelReason = ChannelReason.NONE


def channel_provider(value: str) -> ChannelProvider:
    """Normalize arbitrary provider input without creating a cardinality leak."""

    try:
        return ChannelProvider(value)
    except ValueError:
        return ChannelProvider.OTHER


def channel_operation(value: str) -> ChannelOperation:
    """Normalize arbitrary operation input into the closed operation set."""

    try:
        return ChannelOperation(value)
    except ValueError:
        return ChannelOperation.OTHER


@runtime_checkable
class ChannelTelemetry(Protocol):
    """Synchronous, no-I/O telemetry seam used by Channel hot paths."""

    def message_disposition(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        result: ChannelResult,
        reason: ChannelReason,
    ) -> None: ...

    def queue_depth(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        depth: int,
    ) -> None: ...

    def queue_wait(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        seconds: float,
    ) -> None: ...

    def queue_abandoned(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        count: int,
    ) -> None: ...

    def execution_duration(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        result: ChannelResult,
        seconds: float,
    ) -> None: ...

    def first_visible(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        stage: ChannelStage,
        seconds: float,
    ) -> None: ...

    def delivery(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        stage: ChannelStage,
        result: ChannelResult,
        reason: ChannelReason,
    ) -> None: ...

    def shutdown(
        self,
        *,
        provider: ChannelProvider,
        result: ChannelResult,
        reason: ChannelReason,
        queued: int,
        running: int,
    ) -> None: ...

    def candidate_gc(
        self,
        *,
        result: ChannelResult,
        reason: ChannelReason,
        seconds: float,
        explicit_canvas: int,
        legacy_canvas: int,
        legacy_dialog: int,
        batches: int,
        has_more: bool,
    ) -> None: ...


class NoopChannelTelemetry:
    """Default dependency for components that are not production-composed."""

    def message_disposition(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        result: ChannelResult,
        reason: ChannelReason,
    ) -> None:
        del provider, operation, result, reason

    def queue_depth(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        depth: int,
    ) -> None:
        del provider, operation, depth

    def queue_wait(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        seconds: float,
    ) -> None:
        del provider, operation, seconds

    def queue_abandoned(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        count: int,
    ) -> None:
        del provider, operation, count

    def execution_duration(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        result: ChannelResult,
        seconds: float,
    ) -> None:
        del provider, operation, result, seconds

    def first_visible(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        stage: ChannelStage,
        seconds: float,
    ) -> None:
        del provider, operation, stage, seconds

    def delivery(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        stage: ChannelStage,
        result: ChannelResult,
        reason: ChannelReason,
    ) -> None:
        del provider, operation, stage, result, reason

    def shutdown(
        self,
        *,
        provider: ChannelProvider,
        result: ChannelResult,
        reason: ChannelReason,
        queued: int,
        running: int,
    ) -> None:
        del provider, result, reason, queued, running

    def candidate_gc(
        self,
        *,
        result: ChannelResult,
        reason: ChannelReason,
        seconds: float,
        explicit_canvas: int,
        legacy_canvas: int,
        legacy_dialog: int,
        batches: int,
        has_more: bool,
    ) -> None:
        del result, reason, seconds, explicit_canvas, legacy_canvas, legacy_dialog, batches, has_more


NOOP_CHANNEL_TELEMETRY: ChannelTelemetry = NoopChannelTelemetry()


class InMemoryChannelTelemetry:
    """Bounded per-process counter/histogram recorder with no exporter I/O."""

    def __init__(
        self,
        *,
        max_series: int = 512,
        samples_per_histogram: int = 256,
        recent_events: int = 512,
    ) -> None:
        if max_series < 1 or samples_per_histogram < 1 or recent_events < 1:
            raise ValueError("channel telemetry bounds must be positive")
        self._max_series = max_series
        self._samples_per_histogram = samples_per_histogram
        self._counters: dict[tuple[ChannelMetric, ChannelTelemetryLabels], float] = {}
        self._histograms: dict[tuple[ChannelMetric, ChannelTelemetryLabels], deque[float]] = {}
        self._recent_events: deque[ChannelTelemetryEvent] = deque(maxlen=recent_events)
        self._dropped_series = 0
        self._lock = threading.Lock()

    def message_disposition(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        result: ChannelResult,
        reason: ChannelReason,
    ) -> None:
        self._counter(
            ChannelMetric.MESSAGES_TOTAL,
            self._labels(provider, operation, result, reason, ChannelStage.INGRESS),
        )

    def queue_depth(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        depth: int,
    ) -> None:
        self._histogram(
            ChannelMetric.QUEUE_DEPTH,
            self._labels(provider, operation, ChannelResult.OK, ChannelReason.NONE, ChannelStage.QUEUE),
            float(max(0, depth)),
        )

    def queue_wait(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        seconds: float,
    ) -> None:
        self._histogram(
            ChannelMetric.QUEUE_WAIT_SECONDS,
            self._labels(provider, operation, ChannelResult.OK, ChannelReason.NONE, ChannelStage.QUEUE),
            max(0.0, seconds),
        )

    def queue_abandoned(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        count: int,
    ) -> None:
        if count <= 0:
            return
        self._counter(
            ChannelMetric.QUEUE_ABANDONED_TOTAL,
            self._labels(
                provider,
                operation,
                ChannelResult.DROPPED,
                ChannelReason.QUEUE_ABANDONED,
                ChannelStage.SHUTDOWN,
            ),
            float(count),
        )

    def execution_duration(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        result: ChannelResult,
        seconds: float,
    ) -> None:
        self._histogram(
            ChannelMetric.EXECUTION_DURATION_SECONDS,
            self._labels(provider, operation, result, ChannelReason.NONE, ChannelStage.EXECUTION),
            max(0.0, seconds),
        )

    def first_visible(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        stage: ChannelStage,
        seconds: float,
    ) -> None:
        metric = ChannelMetric.FIRST_CARD_SECONDS if stage is ChannelStage.CARD_CREATE else ChannelMetric.FIRST_CONTENT_SECONDS
        self._histogram(
            metric,
            self._labels(
                provider,
                operation,
                ChannelResult.OK,
                ChannelReason.NONE,
                stage,
            ),
            max(0.0, seconds),
        )

    def delivery(
        self,
        *,
        provider: ChannelProvider,
        operation: ChannelOperation,
        stage: ChannelStage,
        result: ChannelResult,
        reason: ChannelReason,
    ) -> None:
        self._counter(
            ChannelMetric.DELIVERY_TOTAL,
            self._labels(provider, operation, result, reason, stage),
        )

    def shutdown(
        self,
        *,
        provider: ChannelProvider,
        result: ChannelResult,
        reason: ChannelReason,
        queued: int,
        running: int,
    ) -> None:
        labels = self._labels(
            provider,
            ChannelOperation.OTHER,
            result,
            reason,
            ChannelStage.SHUTDOWN,
        )
        self._counter(ChannelMetric.SHUTDOWN_TOTAL, labels)
        if queued > 0:
            self._counter(ChannelMetric.SHUTDOWN_REPLIES_TOTAL, labels, float(queued))
        if running > 0:
            running_labels = self._labels(
                provider,
                ChannelOperation.OTHER,
                result,
                ChannelReason.SHUTDOWN,
                ChannelStage.EXECUTION,
            )
            self._counter(ChannelMetric.SHUTDOWN_REPLIES_TOTAL, running_labels, float(running))

    def candidate_gc(
        self,
        *,
        result: ChannelResult,
        reason: ChannelReason,
        seconds: float,
        explicit_canvas: int,
        legacy_canvas: int,
        legacy_dialog: int,
        batches: int,
        has_more: bool,
    ) -> None:
        labels = self._labels(
            ChannelProvider.INTERNAL,
            ChannelOperation.MAINTENANCE,
            result,
            reason,
            ChannelStage.CANDIDATE_GC,
        )
        self._counter(ChannelMetric.CANDIDATE_GC_CYCLES_TOTAL, labels)
        self._histogram(ChannelMetric.CANDIDATE_GC_DURATION_SECONDS, labels, max(0.0, seconds))
        self._histogram(ChannelMetric.CANDIDATE_GC_BATCHES, labels, float(max(0, batches)))
        for stage, count in (
            (ChannelStage.CANDIDATE_GC_EXPLICIT_CANVAS, explicit_canvas),
            (ChannelStage.CANDIDATE_GC_LEGACY_CANVAS, legacy_canvas),
            (ChannelStage.CANDIDATE_GC_LEGACY_DIALOG, legacy_dialog),
        ):
            if count <= 0:
                continue
            self._counter(
                ChannelMetric.CANDIDATE_GC_DELETED_TOTAL,
                self._labels(
                    ChannelProvider.INTERNAL,
                    ChannelOperation.MAINTENANCE,
                    result,
                    reason,
                    stage,
                ),
                float(count),
            )
        if has_more:
            self._counter(ChannelMetric.CANDIDATE_GC_REMAINING_TOTAL, labels)

    def snapshot(self) -> ChannelTelemetrySnapshot:
        """Return an immutable copy; callers cannot mutate recorder state."""

        with self._lock:
            counters = MappingProxyType(dict(self._counters))
            histograms = MappingProxyType({key: tuple(values) for key, values in self._histograms.items()})
            recent_events = tuple(self._recent_events)
            dropped_series = self._dropped_series
        return ChannelTelemetrySnapshot(
            counters=counters,
            histograms=histograms,
            recent_events=recent_events,
            dropped_series=dropped_series,
        )

    @staticmethod
    def _labels(
        provider: ChannelProvider,
        operation: ChannelOperation,
        result: ChannelResult,
        reason: ChannelReason,
        stage: ChannelStage,
    ) -> ChannelTelemetryLabels:
        return ChannelTelemetryLabels(
            provider=provider,
            operation=operation,
            result=result,
            reason=reason,
            stage=stage,
        )

    def _counter(
        self,
        metric: ChannelMetric,
        labels: ChannelTelemetryLabels,
        value: float = 1.0,
    ) -> None:
        key = (metric, labels)
        event = ChannelTelemetryEvent(metric=metric, labels=labels, value=value)
        with self._lock:
            if key not in self._counters and self._series_count() >= self._max_series:
                self._dropped_series += 1
                return
            self._counters[key] = self._counters.get(key, 0.0) + value
            self._recent_events.append(event)

    def _histogram(
        self,
        metric: ChannelMetric,
        labels: ChannelTelemetryLabels,
        value: float,
    ) -> None:
        key = (metric, labels)
        event = ChannelTelemetryEvent(metric=metric, labels=labels, value=value)
        with self._lock:
            values = self._histograms.get(key)
            if values is None:
                if self._series_count() >= self._max_series:
                    self._dropped_series += 1
                    return
                values = deque(maxlen=self._samples_per_histogram)
                self._histograms[key] = values
            values.append(value)
            self._recent_events.append(event)

    def _series_count(self) -> int:
        return len(self._counters) + len(self._histograms)


# One bounded recorder per process. Constructors still default to the no-op;
# production roots opt in explicitly so tests and embedders cannot accidentally
# acquire global state. There is intentionally no HTTP/Prometheus exporter.
PROCESS_CHANNEL_TELEMETRY = InMemoryChannelTelemetry()
