import logging

import pytest

from api.db.services.task_service import TASK_LOG_MAX_BYTES, _configured_task_log_length, trim_header_by_lines


@pytest.mark.parametrize("value, expected", [(None, 3000), ("4500", 4500), (" 12 ", 12), ("0", 3000), ("-1", 3000), ("", 3000), ("3.5", 3000), ("bad", 3000), ("999999", TASK_LOG_MAX_BYTES)])
def test_configuration_defaults_bounds_and_invalid_values(value: str | None, expected: int) -> None:
    assert _configured_task_log_length(value) == expected


def test_bad_configuration_does_not_echo_untrusted_value(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert _configured_task_log_length("private-untrusted-value") == 3000
    assert "TASK_MAX_LOG_LENGTH" in caplog.text and "private-untrusted-value" not in caplog.text


@pytest.mark.parametrize(
    "text, limit, expected",
    [
        ("short", 10, "short"),
        ("0123456789", 5, "56789"),
        ("prefix\nkeep\nlast", 9, "keep\nlast"),
        ("longheader\nlast", 6, "last"),
        ("longheader\nlast\n", 5, "last\n"),
        ("head\r\nkeep\r\nlast", 10, "keep\r\nlast"),
        ("\nhello", 30, "\nhello"),
        ("", 1, ""),
    ],
)
def test_lines_and_unbroken_messages_have_strict_character_limit(text: str, limit: int, expected: str) -> None:
    assert trim_header_by_lines(text, limit) == expected


def test_unicode_tail_obeys_portable_text_byte_capacity() -> None:
    text = "old\n" + "中文🚀" * 30000
    result = trim_header_by_lines(text, 100000)
    assert len(result.encode("utf-8")) <= TASK_LOG_MAX_BYTES < 65535
    assert text.endswith(result) and result.endswith("中文🚀")
    assert result.encode("utf-8").decode("utf-8") == result


@pytest.mark.parametrize("length", [0, -1])
def test_internal_invalid_length_is_rejected(length: int) -> None:
    with pytest.raises(ValueError):
        trim_header_by_lines("log", length)
