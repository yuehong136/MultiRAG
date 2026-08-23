"""Closed-label and bounded-storage contracts for CHN-O9 telemetry."""

from __future__ import annotations

import inspect

from api.channels.telemetry import (
    NOOP_CHANNEL_TELEMETRY,
    PROCESS_CHANNEL_TELEMETRY,
    ChannelMetric,
    ChannelOperation,
    ChannelProvider,
    ChannelReason,
    ChannelResult,
    ChannelStage,
    ChannelTelemetry,
    InMemoryChannelTelemetry,
    channel_operation,
    channel_provider,
)


def test_channel_telemetry_is_synchronous_and_production_has_bounded_recorder() -> None:
    assert isinstance(NOOP_CHANNEL_TELEMETRY, ChannelTelemetry)
    assert isinstance(PROCESS_CHANNEL_TELEMETRY, InMemoryChannelTelemetry)
    for method_name in (
        "message_disposition",
        "queue_depth",
        "queue_wait",
        "queue_abandoned",
        "execution_duration",
        "first_visible",
        "delivery",
        "shutdown",
        "candidate_gc",
    ):
        assert not inspect.iscoroutinefunction(getattr(NOOP_CHANNEL_TELEMETRY, method_name))
        assert not inspect.iscoroutinefunction(getattr(PROCESS_CHANNEL_TELEMETRY, method_name))


def test_unknown_provider_and_operation_collapse_to_closed_other_labels() -> None:
    assert channel_provider("future-provider-with-user-id") is ChannelProvider.OTHER
    assert channel_operation("future-operation-with-message-id") is ChannelOperation.OTHER
    assert channel_provider("feishu") is ChannelProvider.FEISHU
    assert channel_operation("regenerate") is ChannelOperation.REGENERATE


def test_in_memory_recorder_bounds_series_samples_and_recent_events() -> None:
    recorder = InMemoryChannelTelemetry(
        max_series=2,
        samples_per_histogram=2,
        recent_events=2,
    )
    for seconds in (0.1, 0.2, 0.3):
        recorder.queue_wait(
            provider=ChannelProvider.FEISHU,
            operation=ChannelOperation.MESSAGE,
            seconds=seconds,
        )
    recorder.message_disposition(
        provider=ChannelProvider.FEISHU,
        operation=ChannelOperation.MESSAGE,
        result=ChannelResult.OK,
        reason=ChannelReason.NONE,
    )
    recorder.delivery(
        provider=ChannelProvider.FEISHU,
        operation=ChannelOperation.MESSAGE,
        stage=ChannelStage.CARD_UPDATE,
        result=ChannelResult.FAILED,
        reason=ChannelReason.CARD_UPDATE_FAILURE,
    )

    snapshot = recorder.snapshot()

    assert list(snapshot.histograms.values()) == [(0.2, 0.3)]
    assert list(snapshot.counters.values()) == [1.0]
    assert len(snapshot.recent_events) == 2
    assert snapshot.dropped_series == 1
    assert all(
        isinstance(event.labels.provider, ChannelProvider)
        and isinstance(event.labels.operation, ChannelOperation)
        and isinstance(event.labels.result, ChannelResult)
        and isinstance(event.labels.reason, ChannelReason)
        and isinstance(event.labels.stage, ChannelStage)
        for event in snapshot.recent_events
    )


def test_candidate_gc_records_only_numeric_values_and_closed_categories() -> None:
    recorder = InMemoryChannelTelemetry()

    recorder.candidate_gc(
        result=ChannelResult.OK,
        reason=ChannelReason.NONE,
        seconds=0.25,
        explicit_canvas=2,
        legacy_canvas=1,
        legacy_dialog=0,
        batches=2,
        has_more=True,
    )

    snapshot = recorder.snapshot()
    deleted = [event for event in snapshot.recent_events if event.metric is ChannelMetric.CANDIDATE_GC_DELETED_TOTAL]
    assert [(event.labels.stage, event.value) for event in deleted] == [
        (ChannelStage.CANDIDATE_GC_EXPLICIT_CANVAS, 2.0),
        (ChannelStage.CANDIDATE_GC_LEGACY_CANVAS, 1.0),
    ]
    assert all(event.labels.provider is ChannelProvider.INTERNAL for event in snapshot.recent_events)
    assert all(event.labels.operation is ChannelOperation.MAINTENANCE for event in snapshot.recent_events)
