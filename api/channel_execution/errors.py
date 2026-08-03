"""Safe, classified failures for Channel execution."""

from __future__ import annotations


class ChannelExecutionError(RuntimeError):
    """Base exception carrying only a stable, non-sensitive error code."""

    code = "CHANNEL_EXECUTION_FAILED"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or type(self).code
        super().__init__(self.code)


class BindingNotFoundError(ChannelExecutionError):
    code = "BINDING_NOT_FOUND"


class BindingDisabledError(ChannelExecutionError):
    code = "BINDING_DISABLED"


class TargetTypeUnsupportedError(ChannelExecutionError):
    code = "TARGET_TYPE_UNSUPPORTED"


class TargetRevisionUnavailableError(ChannelExecutionError):
    code = "TARGET_REVISION_UNAVAILABLE"


class TargetExecutionFailedError(ChannelExecutionError):
    code = "TARGET_EXECUTION_FAILED"


class ChannelStateUnavailableError(ChannelExecutionError):
    code = "CHANNEL_STATE_UNAVAILABLE"


class DuplicateEventError(ChannelExecutionError):
    code = "EVENT_ALREADY_CLAIMED"
