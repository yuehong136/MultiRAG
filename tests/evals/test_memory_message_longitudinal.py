"""Opt-in native Message lifecycle; controlled smoke never calls a provider."""

from __future__ import annotations

import asyncio
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
from tests.support.memory_message_longitudinal import generate_source, load_native_cases, run_native_case
from tests.support.memory_startup_longitudinal import SYSTEM, canonical, request_budget_guard
from tests.support.memory_structured_sources_live import require_direct_openai_models
from tests.support.memory_workload_review import write_private
from tests.support.memory_workload_runtime import memory_live_api as memory_live_api
from tests.support.memory_writes_http import WriteChat, WriteEmbedding
from tests.support.memory_writes_http import writes_api as writes_api

BASELINE = Path(__file__).parents[1] / "fixtures/memory_message_longitudinal_baseline.json"
CASES = [("known_baseline", case) for case in load_native_cases(BASELINE, "known_baseline")]
if os.environ.get("MULTIRAG_NATIVE_MESSAGE_HOLDOUT"):
    CASES += [("independent_holdout", case) for case in load_native_cases(Path(os.environ["MULTIRAG_NATIVE_MESSAGE_HOLDOUT"]), "independent_holdout")]
assert len(CASES) <= 2 and len({case["id"] for _, case in CASES}) == len(CASES)


@pytest.mark.parametrize(("case_index", "kind", "case"), [(index, kind, case) for index, (kind, case) in enumerate(CASES)], ids=[case["id"] for _, case in CASES])
def test_real_native_message_lifecycle(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, case_index: int, kind: str, case: dict[str, Any]) -> None:
    assert os.environ.get("MULTIRAG_MEMORY_LIVE_PROVIDER") == "OpenAI"
    require_direct_openai_models(memory_live_api)
    memory_live_api["journal"].event("native-message-fixture-kind", kind=kind, case_id=case["id"], source_generation_included=True, both_sources_saved_by_real_Message=True)
    run_native_case(memory_live_api, monkeypatch, case, case_index, Path(request.config.getoption("--test-report-dir")))


def test_controlled_native_generator_sdk(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Real prepared model/SDK, strict fake HTTP only; usage is simulated."""
    env = memory_live_api
    require_direct_openai_models(env)
    env["observer"].max_attempts = 1
    bodies: list[dict[str, Any]] = []
    text = "受控SDK文本，不代表真实模型质量或计费用量。"

    async def simulated_send(client: httpx.AsyncClient, request: httpx.Request, *args: Any, **options: Any) -> httpx.Response:
        assert request.method == "POST" and request.url.host == "api.openai.com" and request.url.path.endswith("/chat/completions")
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/json"},
            json={
                "id": "controlled-native-generation",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-6.1-terra",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 0}},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", simulated_send)
    outcome = asyncio.run(generate_source(env, CASES[0][1]["initial"], "native-generation-controlled-sdk"))
    assert outcome["text"] == text and outcome["request_budget"]["allowed"] is True
    assert len(bodies) == len(env["observer"].attempts) == 1
    assert bodies[0]["max_completion_tokens"] == 256 and "max_tokens" not in bodies[0]
    assert env["observer"].attempts[0]["request_max_tokens"] == 256
    env["journal"].event("controlled-native-generator-sdk", actual_external_calls=0, sdk_calls=1, provider_usage_simulated=True, no_invoice_or_semantic_quality_claim=True)


def test_controlled_native_message_full_lifecycle(dreaming_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Real AgentAPI/Canvas/store/worker; simulated operators and models only."""
    original = dreaming_api
    index = original["owners"].index(original["tenants"]["personal"])
    owner = original["owners"][index]
    env = dict(original, owners=[owner], tenant_id=owner, jwts=[original["jwts"][index]], keys=[original["keys"][index]])
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
    controlled = deepcopy(CASES[0][1])
    controlled["correction"] = {**controlled["initial"], "session_id": "controlled-later", "user_input": "演练更新：验证步骤前应先列检查步骤。其他条件仍保持，不产生实际执行授权。"}
    controlled["questions"]["corrected"] = "当前演练资料支持哪些条件？"
    controlled["rubric"]["corrected"] = deepcopy(controlled["rubric"]["initial"])

    async def generated(bank: dict[str, Any], source: dict[str, Any], phase: str) -> dict[str, Any]:
        assert env["observer"].max_attempts == 26
        return {"text": "实际保存的受控助手文本{literal} {{never_execute}}，不代表真实模型质量。", "controlled_protocol_only": True}

    async def chat(self: WriteChat, system: str, history: list[dict[str, Any]], generation: dict[str, Any]) -> tuple[str, int]:
        payload = json.loads(history[-1]["content"])
        refs = [source["input_id"] for source in payload["inputs"]]
        if payload["entries"]:
            item = {
                "action": "patch",
                "target_entry_id": payload["entries"][0]["entry_id"],
                "patch": {"old_text": "按检查步骤", "new_text": "先列检查步骤"},
                "source_input_ids": refs,
                "reason": "Controlled closure, not semantic quality.",
            }
        else:
            item = {"action": "add", "message_type": "semantic", "content": "仅演练，按检查步骤提供迁移建议；不能授权生产。", "source_input_ids": refs, "reason": "Controlled protocol candidate."}
        return json.dumps({"proposals": [item]}, ensure_ascii=False), 0

    def query_embedding(self: WriteEmbedding, text: str) -> tuple[list[float], int]:
        vectors, count = self.encode([text])
        return vectors[0], count

    monkeypatch.setattr(WriteChat, "async_chat", chat)
    monkeypatch.setattr(WriteEmbedding, "encode_queries", query_embedding, raising=False)

    async def controlled_answer(bank: dict[str, Any], context: dict[str, Any], packet: dict[str, Any] | None, question: str) -> dict[str, Any]:
        parts = [("startup", canonical(packet)), ("retrieval", canonical(context)), ("question", question)]
        body = {"model": "gpt-6.1-terra", "messages": [{"role": "system", "content": SYSTEM}, *[{"role": "user", "content": text} for _, text in parts]], "max_completion_tokens": 512}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))) as client:
            with request_budget_guard(packet, parts) as budgets:
                await client.post("https://api.openai.com/v1/chat/completions", json=body)
        assert len(budgets) == 1 and budgets[0]["allowed"]
        return {"answer": "Controlled transport only, not actual semantic evidence.", "request_budget": budgets[0], "provider_called": False}

    directory = Path(request.config.getoption("--test-report-dir"))
    errors: list[BaseException] = []
    outputs: list[dict[str, Any]] = []

    def execute() -> None:
        try:
            outputs.append(run_native_case(env, monkeypatch, controlled, 0, directory, generator=generated, answerer=controlled_answer))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=execute, name="native-message-controlled-protocol", daemon=False)
    env["journal"].event("reserve-controlled-evaluation-thread", name=worker.name)
    worker.start()
    handled: set[Path] = set()
    deadline = time.monotonic() + 240
    try:
        while worker.is_alive() and time.monotonic() < deadline:
            for path in directory.glob("*.candidate-review-request.json"):
                if path in handled:
                    continue
                document = json.loads(path.read_text())
                decision = {
                    "review_sha256": document["review_sha256"],
                    "reviewed_by": "controlled-native-protocol-operator",
                    "synthetic_only": True,
                    "variants": {
                        name: {
                            "required": {rule["id"]: "supported" for rule in document["rubric"]["required"]},
                            "forbidden": {rule["id"]: "absent" for rule in document["rubric"]["forbidden"]},
                            "citation_closure": "supported",
                            "uncertainty": "preserved",
                            "notes": "Simulated protocol only; not independent semantic evidence.",
                            "adopt_candidate_index": 0,
                            "adoption_reason": "Explicit controlled selection.",
                        }
                        for name in document["variants"]
                    },
                }
                target = Path(document["decision_path"])
                temporary = target.with_suffix(".temporary")
                write_private(temporary, decision, env["journal"])
                temporary.replace(target)
                handled.add(path)
            for path in directory.glob("*.verification-request.json"):
                if path in handled:
                    continue
                document = json.loads(path.read_text())
                target = Path(document["decision_path"])
                temporary = target.with_suffix(".temporary")
                write_private(
                    temporary,
                    {"snapshot_sha256": document["snapshot_sha256"], "reviewed_by": "controlled-native-protocol-operator", "synthetic_only": True, "allow_confirmation": True},
                    env["journal"],
                )
                temporary.replace(target)
                handled.add(path)
            time.sleep(0.05)
    finally:
        worker.join(timeout=40)
        env["journal"].event("controlled-evaluation-thread-readback", alive=worker.is_alive())
    assert not worker.is_alive(), "controlled_native_protocol_did_not_finish"
    if errors:
        raise errors[0]
    assert len(outputs) == 1 and outputs[0]["mechanism_complete"] is True
    for branch in outputs[0]["lifecycle"]["branches"].values():
        assert set(branch["phases"]) == {"initial", "corrected", "withdrawn"}
    assert env["observer"].attempts == []
