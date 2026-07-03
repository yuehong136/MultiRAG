import pytest

from api.utils.chat_request_validation import validate_chat_messages


def test_validate_chat_messages_accepts_valid_messages():
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "你好"},
    ]

    assert validate_chat_messages(messages) == messages


def test_validate_chat_messages_rejects_missing_content():
    with pytest.raises(ValueError, match=r"messages\[0\] is missing required field 'content'"):
        validate_chat_messages([{"role": "user"}])


def test_validate_chat_messages_rejects_non_string_content():
    with pytest.raises(ValueError, match=r"messages\[0\]\.content must be a string"):
        validate_chat_messages([{"role": "user", "content": {"text": "你好"}}])
