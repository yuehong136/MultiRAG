from typing import Any


def validate_chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for idx, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"messages[{idx}] must be an object.")

        role = message.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"messages[{idx}].role must be a non-empty string.")

        if "content" not in message:
            raise ValueError(f"messages[{idx}] is missing required field 'content'.")

        if not isinstance(message["content"], str):
            raise ValueError(f"messages[{idx}].content must be a string.")

    return messages
