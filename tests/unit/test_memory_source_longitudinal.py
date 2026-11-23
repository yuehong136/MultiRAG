"""Deterministic lifecycle and external-decision contracts, not semantic scoring."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support import memory_source_longitudinal as longitudinal
from tests.support.memory_source_comparison import digest, review_request
from tests.support.memory_startup_longitudinal import SYSTEM, canonical, request_budget_guard

FIXTURE = Path(__file__).parents[1] / "fixtures/memory_source_longitudinal_baseline.json"


def case() -> dict[str, Any]:
    return longitudinal.load_cases(FIXTURE, "known_baseline")[0]


def correction_case() -> dict[str, Any]:
    value = case()
    value["correction"] = deepcopy(value["initial"])
    value["correction"]["session_id"] = "later-session"
    value["questions"]["corrected"] = "Only current supported information?"
    value["rubric"]["corrected"] = deepcopy(value["rubric"]["initial"])
    return value


def decision_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    item = case()
    candidates = [
        {
            "candidate_id": "actual-candidate",
            "action": "add",
            "content": "仅限本主体的条件。",
            "reason": "来源声明",
            "assessment": {"kind": "hypothesis", "verification_status": "unverified"},
            "sources": [{"source_id": 11}],
        }
    ]
    request, _ = review_request(
        {"id": item["id"], "sources": [item["initial"]], "rubric": item["rubric"]["initial"]["candidate"]},
        {"branches": {name: {"state": "succeeded", "sources": [{"source_id": "11"}], "candidates": candidates} for name in longitudinal.BRANCHES}},
    )
    view = {
        "required": {rule["id"]: "supported" for rule in request["rubric"]["required"]},
        "forbidden": {rule["id"]: "absent" for rule in request["rubric"]["forbidden"]},
        "citation_closure": "supported",
        "uncertainty": "preserved",
        "notes": "Controlled decision protocol example, not actual semantic approval.",
        "adopt_candidate_index": 0,
        "adoption_reason": "Explicit controlled operator selection.",
    }
    return request, {"review_sha256": request["review_sha256"], "reviewed_by": "controlled-operator", "synthetic_only": True, "variants": {alias: deepcopy(view) for alias in request["variants"]}}


def test_one_baseline_source_and_exact_38_call_plan() -> None:
    baseline = case()
    assert baseline["correction"] is None
    assert sum(longitudinal.planned_calls(baseline).values()) == 14
    corrected = correction_case()
    assert longitudinal.validate_case(corrected) == corrected
    plan = {key: value + longitudinal.planned_calls(corrected)[key] for key, value in longitudinal.planned_calls(baseline).items()}
    assert plan == {"source_embedding": 6, "publication_embedding": 6, "retrieval_embedding": 10, "analysis": 6, "answer": 10}
    assert sum(plan.values()) == 38 and plan["analysis"] * 1024 + plan["answer"] * 512 == 11264


def test_new_correction_does_not_silently_invent_question_or_rubric() -> None:
    value = case()
    value["correction"] = deepcopy(value["initial"])
    with pytest.raises(ValueError, match="correction"):
        longitudinal.validate_case(value)


def test_applicability_and_phase_rubric_are_not_source_fields() -> None:
    value = case()
    original = deepcopy(value)
    for branch in longitudinal.BRANCHES:
        request = longitudinal.source_request(value["initial"], branch, "source")
        serialized = json.dumps(request, ensure_ascii=False)
        assert value["applicability"] not in serialized
        assert not any(rule["description"] in serialized for categories in value["rubric"]["initial"].values() for rules in categories.values() for rule in rules)
    assert value == original


def test_structurally_valid_semantics_do_not_auto_adopt() -> None:
    request, decision = decision_fixture()
    for view in decision["variants"].values():
        del view["adopt_candidate_index"]
    with pytest.raises(ValueError, match="explicit"):
        longitudinal.validate_adoption(request, decision)


def test_zero_index_is_explicit_and_false_is_not_an_index() -> None:
    request, decision = decision_fixture()
    assert set(longitudinal.validate_adoption(request, decision).values()) == {0}
    decision["variants"]["variant-1"]["adopt_candidate_index"] = False
    with pytest.raises(ValueError, match="selection"):
        longitudinal.validate_adoption(request, decision)


def test_explicit_decline_is_preserved_and_request_hash_is_bound() -> None:
    request, decision = decision_fixture()
    for view in decision["variants"].values():
        view["adopt_candidate_index"] = None
        view["adoption_reason"] = "Decline; do not manufacture a complete lifecycle."
    assert set(longitudinal.validate_adoption(request, decision).values()) == {None}
    request["instruction"] = "changed"
    with pytest.raises(ValueError, match="request_changed"):
        longitudinal.validate_adoption(request, decision)


async def test_reused_actual_wire_guard_counts_complete_startup_and_keeps_conditions(monkeypatch: pytest.MonkeyPatch) -> None:
    packet = {"items": [{"content": "仅内网，不授权生产。", "verification": {"applicability": "仅限该合成主体。"}}], "startup_text": "仅内网，不授权生产。", "usage": {"budget_tokens": 8192}}
    context = {"items": [{"message_id": "11", "content": "独立合法来源仍应保留。"}]}
    parts = [("startup", canonical(packet)), ("retrieval", canonical(context)), ("question", "What does current evidence support?")]
    sent: list[dict[str, Any]] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "controlled"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with request_budget_guard(packet, parts) as budgets:
            await client.post(
                "https://api.openai.com/v1/chat/completions",
                json={"model": "gpt-6.1-terra", "messages": [{"role": "system", "content": SYSTEM}, *[{"role": "user", "content": text} for _, text in parts]], "max_completion_tokens": 512},
            )
    assert len(sent) == len(budgets) == 1 and budgets[0]["allowed"]
    assert sent[0]["messages"][1]["content"] == canonical(packet)
    assert sent[0]["messages"][2]["content"] == canonical(context)
    assert digest(packet) == digest(json.loads(sent[0]["messages"][1]["content"]))
