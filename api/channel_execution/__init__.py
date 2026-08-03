"""Trusted execution boundary shared by MultiRAG channel runtimes."""

from api.channel_execution.models import (
    ChannelActor,
    ChannelExecutionCommand,
    ChannelMessage,
    ExecutionEvent,
    ExecutionTargetRef,
    TrustedChannelContext,
    WorkloadIdentity,
)
from api.channel_execution.registry import TargetExecutorRegistry
from api.channel_execution.service import ChannelExecutionService, PublishedTargetExecutionService

__all__ = [
    "ChannelActor",
    "ChannelExecutionCommand",
    "ChannelExecutionService",
    "ChannelMessage",
    "ExecutionEvent",
    "ExecutionTargetRef",
    "PublishedTargetExecutionService",
    "TargetExecutorRegistry",
    "TrustedChannelContext",
    "WorkloadIdentity",
]
