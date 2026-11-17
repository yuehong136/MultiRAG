"""Native source controls and byte/scope boundaries; no semantic oracle."""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support import memory_message_longitudinal as native
from tests.support import memory_source_longitudinal as longitudinal
from tests.support.memory_message_longitudinal import GENERATION_SYSTEM
from tests.support.memory_source_comparison import BRANCHES, Branch, expected_content

FIXTURE = Path(__file__).parents[1] / "fixtures/memory_message_longitudinal_baseline.json"


def recipe() -> dict[str, Any]:
    return native.load_native_cases(FIXTURE, "known_baseline")[0]


def corrected_recipe() -> dict[str, Any]:
    value = recipe()
    value["correction"] = deepcopy(value["initial"])
    value["correction"]["session_id"] = "later-phase-label"
    value["questions"]["corrected"] = "仅依据当前资料答复？"
    value["rubric"]["corrected"] = deepcopy(value["rubric"]["initial"])
    return value


def observed(source: dict[str, Any], branch: Branch) -> dict[str, Any]:
    content = expected_content(source, branch)
    return {
        "content": content,
        "content_sha256": native.content_hash(content),
        "scope": {"user_id": "authenticated-user", "agent_id": "actual-agent", "session_id": source["session_id"]},
        "valid_at": "2026-11-17 06:07:35",
        "provenance": {
            "role_status": "role_unknown" if branch == "legacy" else "caller_declared",
            "event_time": {"value": None, "status": "unknown"},
            "provenance": {"speaker_identity_verified": False, "content_verified": False},
        },
    }


def test_native_adds_only_three_shared_generation_calls_to_38() -> None:
    first, second = native.native_call_plan(recipe()), native.native_call_plan(corrected_recipe())
    assert sum(first.values()) == 15 and sum(second.values()) == 26
    assert first["source_generation"] + second["source_generation"] == 3
    assert 3 * native.GENERATION_LIMIT + 6 * 1024 + 10 * 512 == 12032


def test_generation_receives_only_frozen_source_recipe_not_rubric() -> None:
    value = recipe()
    before = deepcopy(value)
    messages = native.source_generation_messages(value["initial"])
    assert messages == [{"role": "user", "content": value["initial"]["user_input"]}, {"role": "user", "content": value["initial"]["agent_response"]}]
    rendered = json.dumps(messages, ensure_ascii=False)
    assert value["questions"]["initial"] not in rendered and value["applicability"] not in rendered
    assert all(rule["description"] not in rendered for kind in value["rubric"]["initial"].values() for rules in kind.values() for rule in rules)
    assert value == before


def test_native_does_not_promote_client_event_time() -> None:
    value = recipe()
    value["initial"]["event_time"] = "2026-11-17T06:00:00Z"
    value["initial"]["user_input"] += "2026-11-17T06:00:00Z"
    with pytest.raises(ValueError, match="remain_unknown"):
        native.validate_native_case(value)


@pytest.mark.parametrize("field", ["user_id", "agent_id", "session_id", "valid_at", "content"])
def test_exact_pair_rejects_changed_identity_time_or_bytes(field: str) -> None:
    source = recipe()["initial"]
    source["agent_response"] = "真实生成原文{literal}，条件不删。"
    source["session_id"] = "actual-server-session"
    left, right = observed(source, "legacy"), observed(source, "structured")
    native.verify_native_pair(source, left, right, subject="authenticated-user", agent_id="actual-agent")
    if field in right["scope"]:
        right["scope"][field] = "foreign"
    else:
        right[field] = "changed"
    with pytest.raises(AssertionError):
        native.verify_native_pair(source, left, right, subject="authenticated-user", agent_id="actual-agent")


async def test_generation_wire_budget_is_checked_before_transport() -> None:
    sent: list[dict[str, Any]] = []
    messages = native.source_generation_messages(recipe()["initial"])

    async def send(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    body = {"model": "gpt-6.1-terra", "messages": [{"role": "system", "content": GENERATION_SYSTEM}, *messages], "max_completion_tokens": 256}
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with native.generation_wire_guard(messages) as budgets:
            await client.post("https://api.openai.com/v1/chat/completions", json=body)
    assert len(sent) == len(budgets) == 1 and budgets[0]["allowed"]
    assert budgets[0]["output_reserve_tokens"] == 256 and budgets[0]["target_tokenizer_exact"] is False
    body["max_completion_tokens"] = 257
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with native.generation_wire_guard(messages), pytest.raises(AssertionError):
            await client.post("https://api.openai.com/v1/chat/completions", json=body)
    assert len(sent) == 1


def test_lifecycle_sets_entire_cap_before_injected_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Observer:
        attempts: list[dict[str, Any]] = []
        max_attempts = 0

    env: dict[str, Any] = {"observer": Observer(), "journal": object()}
    calls: list[str] = []

    @contextmanager
    def banks(_: dict[str, Any]) -> Any:
        yield {name: {"memory_id": name} for name in BRANCHES}

    def injected(peers: dict[Branch, dict[str, Any]], patch: pytest.MonkeyPatch, case: dict[str, Any], branch: Branch, phase: str, state: dict[str, Any]) -> None:
        assert env["observer"].max_attempts == 15 and set(peers) == set(BRANCHES)
        calls.append(branch)
        state["state"] = "controlled_source_failure"
        return None

    monkeypatch.setattr(longitudinal, "peer_banks", banks)
    monkeypatch.setattr(longitudinal, "write_private", lambda *a, **k: None)
    monkeypatch.setattr(longitudinal, "costs", lambda env: {})
    report = longitudinal.run_lifecycle(env, monkeypatch, recipe(), 0, tmp_path, source_producer=injected, source_generation_calls=1)
    assert set(calls) == set(BRANCHES) and report["plan"]["source_generation"] == 1
    assert report["mechanism_complete"] is False
    for invalid in (True, -1, 3):
        with pytest.raises(ValueError, match="generation_plan"):
            longitudinal.run_lifecycle(env, monkeypatch, recipe(), 0, tmp_path, source_producer=injected, source_generation_calls=invalid)


def test_native_clock_is_scoped_and_restores_real_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    module = native.memory_write_runtime
    original = module.datetime
    with native.paired_reference_clock(monkeypatch, "initial"):
        assert module.current_timestamp() == int(native.REFERENCE_TIME.timestamp() * 1000)
        assert module.datetime.now(native.UTC) == native.REFERENCE_TIME
    assert module.datetime is original
