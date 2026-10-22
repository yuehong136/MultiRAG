"""Prompt fitting preserves the current turn and reports actual retained tokens."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from agent.component.agent_with_tools import Agent
from common.token_utils import encoder, num_tokens_from_string
from core.prompts.generator import message_fit_in


def _text_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(num_tokens_from_string(message["content"]) for message in messages)


@pytest.mark.parametrize("budget", [1, 2, 8, 24, 64])
@pytest.mark.parametrize("system", ["system " * 3, "system " * 500])
def test_reserves_system_or_current_turn_and_counts_actual_text(budget: int, system: str) -> None:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "user " * 80}]
    original = deepcopy(messages)
    used, fitted = message_fit_in(messages, budget)
    assert used == _text_tokens(fitted) <= budget
    assert [message["role"] for message in fitted] == ["system", "user"]
    assert messages == original
    if num_tokens_from_string(system) / _text_tokens(messages) <= 0.8:
        assert num_tokens_from_string(fitted[0]["content"]) == min(num_tokens_from_string(system), budget)
        if num_tokens_from_string(system) <= budget:
            assert fitted[0]["content"] == system
    else:
        assert fitted[-1]["content"]


@pytest.mark.parametrize("budget", [0, -1])
def test_unknown_context_uses_upstream_followup_default(budget: int) -> None:
    messages = [{"role": "system", "content": "s " * 4000}, {"role": "user", "content": "User query"}]
    used, fitted = message_fit_in(messages, budget)
    assert used == _text_tokens(messages) and fitted == messages


@pytest.mark.parametrize("role", ["system", "user", "assistant"])
def test_single_message_and_no_system(role: str) -> None:
    used, fitted = message_fit_in([{"role": role, "content": "one " * 40}], 3)
    assert len(fitted) == 1 and used == _text_tokens(fitted) <= 3


def test_empty_zero_count_and_exact_budget_do_not_drop_history() -> None:
    assert message_fit_in([], 0) == (0, [])
    empty = [{"role": "system", "content": ""}, {"role": "user", "content": ""}]
    assert message_fit_in(empty, 0) == (0, empty)
    messages = [{"role": "system", "content": "system"}, {"role": "assistant", "content": "old"}, {"role": "user", "content": "user"}]
    assert message_fit_in(messages, _text_tokens(messages)) == (_text_tokens(messages), messages)


def test_multiple_systems_not_duplicated_and_all_budgeted() -> None:
    messages = [{"role": "system", "content": "first " * 10}, {"role": "user", "content": "history " * 100}, {"role": "system", "content": "second " * 20}]
    used, fitted = message_fit_in(messages, 10)
    assert len(fitted) == 2 and used == _text_tokens(fitted) <= 10


def test_multimodal_text_truncates_without_dropping_image_or_metadata() -> None:
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,example"}}
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": [image, {"type": "text", "text": "hello " * 80}], "name": "user"}]
    used, fitted = message_fit_in(messages, 6)
    assert used <= 6
    assert fitted[-1]["content"][0] == image and fitted[-1]["name"] == "user"
    assert used == num_tokens_from_string(fitted[0]["content"]) + num_tokens_from_string(fitted[-1]["content"][1]["text"])


def test_tool_continuation_keeps_call_and_sibling_results() -> None:
    call = {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "history " * 300}, call, {"role": "tool", "tool_call_id": "x", "content": "result " * 300}]
    used, fitted = message_fit_in(messages, 90)
    assert used <= 90 and [message["role"] for message in fitted] == ["system", "assistant", "tool"]
    assert fitted[1] == call and fitted[-1]["tool_call_id"] == "x"
    with pytest.raises(ValueError, match="Tool-call payload"):
        message_fit_in(messages, 1)


def test_unicode_trimming_reports_reencoded_tokens() -> None:
    for budget in range(1, 12):
        used, fitted = message_fit_in([{"role": "user", "content": "你好😀世界" * 10}], budget)
        assert used == len(encoder.encode(fitted[0]["content"])) <= budget


def test_agent_active_fit_call_respects_system_budget() -> None:
    agent = Agent.__new__(Agent)
    agent.chat_mdl = SimpleNamespace(max_length=16)
    fitted = agent._fit_messages("system " * 3, [{"role": "user", "content": "question " * 60}])
    assert _text_tokens(fitted) <= int(16 * 0.97)
    assert fitted[0]["content"] == "system " * 3
