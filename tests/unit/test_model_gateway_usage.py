"""Usage extraction from upstream bodies and SSE streams."""

import json

import pytest

from api.model_gateway.usage import UNAVAILABLE, SSEUsageTap, usage_from_payload

_DELTA = b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"hi"}}],"usage":null}\n\n'
_USAGE_ONLY = b'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
_DONE = b"data: [DONE]\n\n"
_STREAM = _DELTA + _USAGE_ONLY + _DONE


def _run(tap: SSEUsageTap, chunks: list[bytes]) -> bytes:
    return b"".join(tap.feed(chunk) for chunk in chunks) + tap.flush()


def test_reported_usage_is_read_with_its_details():
    usage = usage_from_payload({"usage": {"prompt_tokens": 7, "completion_tokens": 0, "total_tokens": 7, "prompt_tokens_details": {"cached_tokens": 4}}})

    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (7, 0, 7)
    assert usage.details == {"prompt_tokens_details": {"cached_tokens": 4}}
    assert usage.source == "reported"


def test_embedding_usage_leaves_completion_unknown():
    usage = usage_from_payload({"usage": {"prompt_tokens": 9, "total_tokens": 9}})

    assert usage.completion_tokens is None
    assert usage.source == "reported"


@pytest.mark.parametrize("payload", [{}, {"usage": None}, {"usage": "5"}, {"usage": {}}, {"usage": {"prompt_tokens": True, "total_tokens": -1}}, [], None])
def test_missing_or_malformed_usage_is_unavailable_not_zero(payload):
    usage = usage_from_payload(payload)

    assert usage is UNAVAILABLE
    assert usage.total_tokens is None


def test_usage_only_chunk_is_hidden_when_the_gateway_asked_for_it():
    tap = SSEUsageTap(hide_usage_only_chunk=True)

    assert _run(tap, [_STREAM]) == _DELTA + _DONE
    assert tap.usage.total_tokens == 5


def test_usage_only_chunk_is_forwarded_when_the_caller_asked_for_it():
    tap = SSEUsageTap(hide_usage_only_chunk=False)

    assert _run(tap, [_STREAM]) == _STREAM
    assert tap.usage.total_tokens == 5


@pytest.mark.parametrize("size", [1, 7, 64])
def test_events_split_across_chunks_give_the_same_bytes(size):
    tap = SSEUsageTap(hide_usage_only_chunk=True)
    chunks = [_STREAM[index : index + size] for index in range(0, len(_STREAM), size)]

    assert _run(tap, chunks) == _DELTA + _DONE
    assert tap.usage.prompt_tokens == 3


def test_crlf_delimited_events_are_recognised():
    stream = _STREAM.replace(b"\n", b"\r\n")
    tap = SSEUsageTap(hide_usage_only_chunk=True)

    assert _run(tap, [stream]) == (_DELTA + _DONE).replace(b"\n", b"\r\n")
    assert tap.usage.completion_tokens == 2


def test_chunk_with_choices_and_usage_is_never_hidden():
    final = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    tap = SSEUsageTap(hide_usage_only_chunk=True)

    assert _run(tap, [final + _DONE]) == final + _DONE
    assert tap.usage.total_tokens == 2


def test_stream_without_usage_stays_unavailable():
    tap = SSEUsageTap(hide_usage_only_chunk=True)

    assert _run(tap, [_DELTA + _DONE]) == _DELTA + _DONE
    assert tap.usage is UNAVAILABLE


def test_unterminated_tail_is_flushed():
    tail = b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'
    tap = SSEUsageTap(hide_usage_only_chunk=False)

    assert tap.feed(_DELTA + tail) == _DELTA
    assert tap.flush() == tail
    assert tap.usage.total_tokens == 2


def test_malformed_event_is_forwarded_untouched():
    broken = b'data: {"usage": {not json\n\n'
    tap = SSEUsageTap(hide_usage_only_chunk=True)

    assert _run(tap, [broken + _DONE]) == broken + _DONE
    assert tap.usage is UNAVAILABLE


def test_oversized_event_without_delimiter_does_not_accumulate():
    blob = b"x" * ((1 << 20) + 1)
    tap = SSEUsageTap(hide_usage_only_chunk=True)

    assert tap.feed(blob) == blob
    assert tap.flush() == b""


def test_forwarded_events_remain_valid_json_lines():
    tap = SSEUsageTap(hide_usage_only_chunk=True)

    forwarded = _run(tap, [_STREAM])

    events = [line[6:] for line in forwarded.split(b"\n\n") if line]
    assert json.loads(events[0])["choices"][0]["delta"]["content"] == "hi"
    assert events[1] == b"[DONE]"


def test_tap_reports_when_the_terminating_event_has_passed():
    tap = SSEUsageTap(hide_usage_only_chunk=True)

    tap.feed(_DELTA + _USAGE_ONLY)
    assert tap.finished is False

    tap.feed(_DONE)
    assert tap.finished is True


def test_done_inside_content_is_not_the_terminator():
    quoted = b'data: {"choices":[{"index":0,"delta":{"content":"data: [DONE]"}}],"usage":null}\n\n'
    tap = SSEUsageTap(hide_usage_only_chunk=True)

    assert tap.feed(quoted) == quoted
    assert tap.finished is False
