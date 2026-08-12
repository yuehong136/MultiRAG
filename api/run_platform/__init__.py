"""Durable Run Platform v2 domain and machine-readable protocol.

The package intentionally has no route registration or runtime side effects in
RUN-F1.  Identity construction and policy decisions remain external ports owned
by the enterprise identity program.
"""

from api.run_platform.domain import RunLifecycle, RunStatus, RunTransitionError
from api.run_platform.events import RunProjection, apply_run_event
from api.run_platform.schemas import (
    RunEventEnvelope,
    parse_event_envelope_json,
    parse_message_delta,
    parse_message_delta_json,
)

__all__ = [
    "RunEventEnvelope",
    "RunLifecycle",
    "RunProjection",
    "RunStatus",
    "RunTransitionError",
    "apply_run_event",
    "parse_event_envelope_json",
    "parse_message_delta",
    "parse_message_delta_json",
]
