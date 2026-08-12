"""Canonical identity-free Run Platform v2 wire schemas for RUN-F1a.

Only shapes already frozen by the approved contract are concrete here. Event
types whose ``data`` schema still belongs to a later target/EIM decision remain
valid v2 envelopes and are not prematurely modelled as typed payloads.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
)


def _require_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Run Platform timestamps must include a timezone.")
    return value


AwareDatetime = Annotated[datetime, AfterValidator(_require_aware_datetime)]


class RunContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


EventTypeT = TypeVar("EventTypeT", bound=str)
EventDataT = TypeVar("EventDataT")


class _RunEventEnvelopeBase(RunContractModel, Generic[EventTypeT, EventDataT]):
    v: Literal[2]
    event_id: str
    run_id: str
    thread_id: str
    turn_id: str
    seq: Annotated[int, Field(ge=1)]
    type: EventTypeT
    created_at: AwareDatetime
    data: EventDataT


class RunEventEnvelope(_RunEventEnvelopeBase[str, dict[str, JsonValue]]):
    """The stable identity-free v2 envelope for additive event types."""

    @field_validator("data")
    @classmethod
    def reject_non_finite_numbers(
        cls,
        value: dict[str, object],
    ) -> dict[str, object]:
        """Reject jiter's non-standard NaN/Infinity extension recursively."""

        def require_finite(item: object) -> None:
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Run event data must contain finite JSON numbers.")
            if isinstance(item, list):
                for child in item:
                    require_finite(child)
            elif isinstance(item, dict):
                for child in item.values():
                    require_finite(child)

        require_finite(value)
        return value


class MessageDeltaData(RunContractModel):
    part_id: str
    delta: str


class MessageDeltaEvent(_RunEventEnvelopeBase[Literal["message.delta"], MessageDeltaData]):
    """The concrete message.delta envelope frozen by RUN-F1a."""


MESSAGE_DELTA_EVENT_ADAPTER = TypeAdapter(MessageDeltaEvent)
RUN_EVENT_ENVELOPE_ADAPTER = TypeAdapter(RunEventEnvelope)


def parse_event_envelope(
    payload: object,
) -> RunEventEnvelope:
    """Validate a Python-native v2 envelope without coercion."""

    return RUN_EVENT_ENVELOPE_ADAPTER.validate_python(payload)


def parse_event_envelope_json(
    payload: str | bytes | bytearray,
) -> RunEventEnvelope:
    """Validate a JSON wire envelope while preserving additive event types."""

    return RUN_EVENT_ENVELOPE_ADAPTER.validate_json(payload)


def parse_message_delta(payload: object) -> MessageDeltaEvent:
    """Validate a Python-native message.delta event without coercion."""

    envelope = parse_event_envelope(payload)
    if envelope.type != "message.delta":
        raise ValueError(f"Expected message.delta, got {envelope.type}.")
    return MESSAGE_DELTA_EVENT_ADAPTER.validate_python(envelope.model_dump(mode="python"))


def parse_message_delta_json(payload: str | bytes | bytearray) -> MessageDeltaEvent:
    """Validate the concrete message.delta shape from its JSON wire form."""

    envelope = parse_event_envelope_json(payload)
    if envelope.type != "message.delta":
        raise ValueError(f"Expected message.delta, got {envelope.type}.")
    return MESSAGE_DELTA_EVENT_ADAPTER.validate_json(payload)
