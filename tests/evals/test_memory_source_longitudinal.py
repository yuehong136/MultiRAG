"""Explicit same-text source replay through human adoption and startup answers."""

from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from api.db.db_models import Memory
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_dreaming_http import dreaming_api as dreaming_api
from tests.support.memory_source_longitudinal import load_cases, run_lifecycle
from tests.support.memory_startup_longitudinal import SYSTEM, canonical, request_budget_guard
from tests.support.memory_structured_sources_live import require_direct_openai_models
from tests.support.memory_workload_review import write_private
from tests.support.memory_workload_runtime import memory_live_api as memory_live_api
from tests.support.memory_writes_http import WriteChat, WriteEmbedding
from tests.support.memory_writes_http import writes_api as writes_api

BASELINE = Path(__file__).parents[1] / "fixtures/memory_source_longitudinal_baseline.json"
CASES = [("known_baseline", item) for item in load_cases(BASELINE, "known_baseline")]
if os.environ.get("MULTIRAG_SOURCE_LONGITUDINAL_HOLDOUT"):
    CASES += [("independent_holdout", item) for item in load_cases(Path(os.environ["MULTIRAG_SOURCE_LONGITUDINAL_HOLDOUT"]), "independent_holdout")]
assert len(CASES) <= 2 and len({item["id"] for _, item in CASES}) == len(CASES)


@pytest.mark.parametrize(("case_index", "fixture_kind", "case"), [(i, kind, item) for i, (kind, item) in enumerate(CASES)], ids=[item["id"] for _, item in CASES])
def test_real_source_longitudinal(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, case_index: int, fixture_kind: str, case: dict[str, Any]) -> None:
    assert os.environ.get("MULTIRAG_MEMORY_LIVE_PROVIDER") == "OpenAI"
    env = memory_live_api
    require_direct_openai_models(env)
    env["journal"].event("longitudinal-fixture-kind", fixture_kind=fixture_kind, case_id=case["id"], input_is_prescribed=True, no_native_Message_generation=True)
    run_lifecycle(env, monkeypatch, case, case_index, Path(request.config.getoption("--test-report-dir")))


def test_controlled_two_bank_longitudinal_smoke(dreaming_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Real APIs/store/worker with simulated operators and no provider execution."""
    original = dreaming_api
    owner = original["owners"].index(original["tenants"]["personal"])
    env = dict(original, tenant_id=original["tenants"]["personal"], jwts=[original["jwts"][owner]], keys=[original["keys"][owner]])
    with Session(env["engine"]) as db:
        memory = db.get(Memory, env["memory_id"])
        assert memory is not None
        env["model_ids"] = {"embedding": memory.tenant_embd_id, "chat": memory.tenant_llm_id}

    class Observer:
        attempts: list[dict[str, Any]] = []
        phase, stage, max_attempts = "", "", 0

        def set_stage(self, stage: str, *, phase: str = "") -> None:
            self.stage, self.phase = stage, phase

    env["observer"] = Observer()
    sample = deepcopy(CASES[0][1])
    # This controlled protocol-only correction is never a paid fixture or gold.
    sample["correction"] = {**sample["initial"], "session_id": "controlled-later", "user_input": "演练更新：最后的示例应为相关示例。原有条件和安全边界继续保留。"}
    sample["questions"]["corrected"] = "What does the current controlled evidence state?"
    sample["rubric"]["corrected"] = deepcopy(sample["rubric"]["initial"])

    async def chat(self: WriteChat, system: str, history: list[dict[str, Any]], generation: dict[str, Any]) -> tuple[str, int]:
        payload = json.loads(history[-1]["content"])
        references = [item["input_id"] for item in payload["inputs"]]
        if payload["entries"]:
            proposal = {
                "action": "patch",
                "target_entry_id": payload["entries"][0]["entry_id"],
                "patch": {"old_text": "最后示例", "new_text": "最后相关示例"},
                "source_input_ids": references,
                "reason": "Controlled host-closure correction.",
            }
        else:
            proposal = {
                "action": "add",
                "content": "先结论，再检查，最后示例；Python仅限Python服务，不授权生产。",
                "message_type": "semantic",
                "source_input_ids": references,
                "reason": "Controlled protocol candidate, not model quality evidence.",
            }
        return json.dumps({"proposals": [proposal]}, ensure_ascii=False), 0

    def query_embedding(self: WriteEmbedding, text: str) -> tuple[list[float], int]:
        vectors, tokens = self.encode([text])
        return vectors[0], tokens

    monkeypatch.setattr(WriteChat, "async_chat", chat)
    monkeypatch.setattr(WriteEmbedding, "encode_queries", query_embedding, raising=False)

    async def controlled_answer(bank: dict[str, Any], context: dict[str, Any], packet: dict[str, Any] | None, question: str) -> dict[str, Any]:
        parts = [("startup", canonical(packet)), ("retrieval", canonical(context)), ("question", question)]
        body = {"model": "gpt-6.1-terra", "messages": [{"role": "system", "content": SYSTEM}, *[{"role": "user", "content": text} for _, text in parts]], "max_completion_tokens": 512}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"controlled": True}))) as client:
            with request_budget_guard(packet, parts) as budgets:
                await client.post("https://api.openai.com/v1/chat/completions", json=body)
        assert len(budgets) == 1 and budgets[0]["allowed"]
        return {"answer": "Controlled protocol response; no semantic accuracy assertion.", "request_budget": budgets[0], "provider_called": False}

    directory = Path(request.config.getoption("--test-report-dir"))
    errors: list[BaseException] = []
    outputs: list[dict[str, Any]] = []

    def execute() -> None:
        try:
            outputs.append(run_lifecycle(env, monkeypatch, sample, 0, directory, answerer=controlled_answer))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=execute, name="controlled-longitudinal-operator-fixture", daemon=False)
    env["journal"].event("reserve-controlled-evaluation-thread", name=worker.name)
    worker.start()
    completed: set[Path] = set()
    deadline = time.monotonic() + 240
    try:
        while worker.is_alive() and time.monotonic() < deadline:
            for path in directory.glob("*.candidate-review-request.json"):
                if path in completed:
                    continue
                document = json.loads(path.read_text())
                decision = {"review_sha256": document["review_sha256"], "reviewed_by": "controlled-protocol-operator", "synthetic_only": True, "variants": {}}
                for alias, view in document["variants"].items():
                    decision["variants"][alias] = {
                        "required": {rule["id"]: "supported" for rule in document["rubric"]["required"]},
                        "forbidden": {rule["id"]: "absent" for rule in document["rubric"]["forbidden"]},
                        "citation_closure": "supported",
                        "uncertainty": "preserved",
                        "notes": "Simulated operator protocol only; not independent semantic evidence.",
                        "adopt_candidate_index": 0 if view["candidates"] else None,
                        "adoption_reason": "Explicit controlled fixture selection.",
                    }
                target = Path(document["decision_path"])
                temporary = target.with_suffix(".temporary")
                write_private(temporary, decision, env["journal"])
                temporary.replace(target)
                completed.add(path)
            for path in directory.glob("*.verification-request.json"):
                if path in completed:
                    continue
                document = json.loads(path.read_text())
                target = Path(document["decision_path"])
                temporary = target.with_suffix(".temporary")
                write_private(
                    temporary, {"snapshot_sha256": document["snapshot_sha256"], "reviewed_by": "controlled-protocol-operator", "synthetic_only": True, "allow_confirmation": True}, env["journal"]
                )
                temporary.replace(target)
                completed.add(path)
            time.sleep(0.05)
    finally:
        worker.join(timeout=40)
        env["journal"].event("controlled-evaluation-thread-readback", alive=worker.is_alive())
    assert not worker.is_alive(), "controlled_operator_fixture_did_not_finish"
    if errors:
        raise errors[0]
    assert len(outputs) == 1 and outputs[0]["mechanism_complete"] is True
    assert all(set(value["phases"]) == {"initial", "corrected", "withdrawn"} for value in outputs[0]["branches"].values())
    assert env["observer"].attempts == [], "Controlled fixtures must not call a paid provider"
