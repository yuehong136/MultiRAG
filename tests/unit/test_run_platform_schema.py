"""Machine-readable RUN-F1a schema and compatibility contracts."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from api.run_platform.schemas import (
    MessageDeltaEvent,
    parse_event_envelope,
    parse_event_envelope_json,
    parse_message_delta,
    parse_message_delta_json,
)
from scripts.generate_run_platform_schema import (
    DEFAULT_OUTPUT_DIR,
    generate_schemas,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "run_platform" / "v2"


def _base_event() -> dict:
    return {
        "v": 2,
        "event_id": "evt_1",
        "run_id": "run_1",
        "thread_id": "thread_1",
        "turn_id": "turn_1",
        "seq": 1,
        "type": "message.delta",
        "created_at": datetime(2026, 8, 13, tzinfo=UTC),
        "data": {"part_id": "answer", "delta": "hello"},
    }


def test_event_envelope_requires_all_nine_fields_v2_timezone_and_no_extra() -> None:
    payload = _base_event()
    assert parse_event_envelope(payload).v == 2

    for field in payload:
        incomplete = dict(payload)
        incomplete.pop(field)
        with pytest.raises(ValidationError):
            parse_event_envelope(incomplete)
    with pytest.raises(ValidationError):
        parse_event_envelope({**payload, "v": 3})
    with pytest.raises(ValidationError):
        parse_event_envelope({**payload, "created_at": datetime(2026, 8, 13)})
    with pytest.raises(ValidationError):
        parse_event_envelope({**payload, "internal_lease": "secret"})
    with pytest.raises(ValidationError):
        parse_event_envelope({**payload, "data": {"value": object()}})
    with pytest.raises(ValidationError):
        parse_event_envelope({**payload, "data": {"value": float("nan")}})


def test_message_delta_is_concrete_but_other_data_remains_additive() -> None:
    assert isinstance(parse_message_delta(_base_event()), MessageDeltaEvent)

    invalid = _base_event()
    invalid["data"] = {
        "part_id": "answer",
        "delta": "hello",
        "raw_prompt": "must not pass",
    }
    with pytest.raises(ValidationError):
        parse_message_delta(invalid)

    future = {
        **_base_event(),
        "type": "usage.updated",
        "data": {"input_tokens": 1},
    }
    assert parse_event_envelope(future).type == "usage.updated"
    with pytest.raises(ValueError, match=r"Expected message\.delta"):
        parse_message_delta(future)


def test_golden_fixtures_preserve_additive_consumer_compatibility() -> None:
    message_delta = (FIXTURE_ROOT / "message-delta.json").read_text(encoding="utf-8")
    unknown_event = (FIXTURE_ROOT / "unknown-additive-event.json").read_text(encoding="utf-8")

    assert parse_message_delta_json(message_delta).data.delta == "hello"
    assert parse_event_envelope_json(unknown_event).type == "usage.updated"

    invalid_seq = json.loads(message_delta)
    invalid_seq["seq"] = "4"
    with pytest.raises(ValidationError):
        parse_message_delta_json(json.dumps(invalid_seq))

    for non_finite in ("NaN", "Infinity", "-Infinity"):
        invalid_number = unknown_event.replace(
            '"input_tokens": 3',
            f'"input_tokens": {non_finite}',
        )
        with pytest.raises(ValidationError):
            parse_event_envelope_json(invalid_number)


def test_generated_json_schemas_have_no_drift() -> None:
    assert generate_schemas(DEFAULT_OUTPUT_DIR, check=True) == []

    envelope_schema = json.loads((DEFAULT_OUTPUT_DIR / "run-event-envelope.schema.json").read_text(encoding="utf-8"))
    assert set(envelope_schema["required"]) == set(_base_event())
    assert envelope_schema["title"] == "RunEventEnvelope"
