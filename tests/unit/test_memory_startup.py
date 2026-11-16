"""Startup schema and complete-envelope budget mechanics, no external services."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from api.apps.restful_apis.memory_startup_api import StartupContextRequest
from common.token_utils import encoder
from memory.services.startup import assemble_startup_package


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": None},
        {"agent_id": None},
        {"budget_tokens": True},
        {"inventory_limit": False},
        {"max_items": 17},
        {"entry_ids": []},
        {"entry_ids": ["a" * 32, "a" * 32]},
        {"cache": True},
        {"verified": True},
        {"user_id": "中" * 100},
    ],
)
def test_strict_startup_request_rejects_ambiguous_scope_or_authority(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StartupContextRequest.model_validate({"user_id": "", "agent_id": "", **changes})


def test_exact_empty_selectors_and_explicit_null_session_are_preserved() -> None:
    value = StartupContextRequest.model_validate({"user_id": "", "agent_id": " ", "session_id": ""})
    assert value.user_id == "" and value.agent_id == " " and value.session_id == ""
    assert StartupContextRequest(user_id="", agent_id="").session_id is None


def test_estimate_counts_every_domain_field_including_repeated_text_and_its_own_counter() -> None:
    item: dict[str, Any] = {"entry_id": "a" * 32, "content": "保留完整范围；禁止生产。", "verification": {"applicability": "仅适用于演练环境。", "checked_at_is_user_reported": False}}
    selection = {"mode": "explicit", "order": "entry_id_asc", "inventory_limit": 64, "scanned_count": 1, "inventory_truncated": False, "physical_read_limit": 256, "read_limited": False}

    def tokens(value: str) -> int:
        return len(encoder.encode(value, disallowed_special=()))

    packed = assemble_startup_package("bank", {"user_id": "", "agent_id": "", "session_id": None}, [item], [], selection, budget_tokens=1024, max_items=1, count_tokens=tokens)
    text = json.dumps(packed, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    assert packed["usage"]["estimated_tokens"] == tokens(text) <= 1024
    assert packed["startup_text"].count(item["content"]) == 1
    assert text.count(item["content"]) == 2


def test_budget_defer_never_claims_selection_complete() -> None:
    item: dict[str, Any] = {"entry_id": "a" * 32, "content": "完整条件" * 2000, "verification": {"applicability": "仅隔离演练。"}}
    selection = {"mode": "explicit", "order": "entry_id_asc", "inventory_limit": 64, "scanned_count": 1, "inventory_truncated": False, "physical_read_limit": 256, "read_limited": False}
    packet = assemble_startup_package("bank", {"user_id": "", "agent_id": "", "session_id": None}, [item], [], selection, budget_tokens=1024, max_items=1, count_tokens=len)
    assert packet["items"] == [] and packet["omitted"]["counts"] == {"budget": 1}
    assert not packet["selection"]["selection_complete"]
