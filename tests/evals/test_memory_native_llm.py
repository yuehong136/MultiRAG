"""Explicit opt-in native generation comparison, with bounded SDK work."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support import memory_compaction_evaluation as compaction
from tests.support import memory_native_llm_evaluation as native
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_structured_sources_live import require_direct_openai_models
from tests.support.memory_workload_runtime import memory_live_api as memory_live_api

BASELINE = Path(__file__).parents[1] / "fixtures/memory_native_llm_baseline.json"
CASES = [("known_baseline", case) for case in native.load_cases(BASELINE, "known_baseline")]
if os.environ.get("MULTIRAG_NATIVE_LLM_HOLDOUT"):
    CASES += [("independent_holdout", case) for case in native.load_cases(Path(os.environ["MULTIRAG_NATIVE_LLM_HOLDOUT"]), "independent_holdout")]
assert len(CASES) <= 2 and len({case["id"] for _, case in CASES}) == len(CASES)


@pytest.mark.parametrize(("case_index", "kind", "case"), [(index, kind, case) for index, (kind, case) in enumerate(CASES)], ids=[case["id"] for _, case in CASES])
def test_real_native_llm(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, case_index: int, kind: str, case: dict[str, Any]) -> None:
    require_direct_openai_models(memory_live_api)
    memory_live_api["journal"].event("native-llm-eval-case", kind=kind, case_id=case["id"], SDK_maximum=5, completion_reserve=2560)
    report = native.run_case(memory_live_api, monkeypatch, case, case_index, Path(request.config.getoption("--test-report-dir")))
    assert report["mechanism_complete"] is True
    assert report["semantic_answers_accepted"] is True, "independent_native_answer_quality_rejected"


class SyntheticStream(httpx.AsyncByteStream):
    def __init__(self, value: bytes) -> None:
        self.value = value

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.value), 13):
            yield self.value[offset : offset + 13]


@pytest.mark.parametrize("case_index", [0, 1], ids=["stream-reference", "nonstream-template"])
def test_controlled_native_llm(memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, case_index: int) -> None:
    env = memory_live_api
    require_direct_openai_models(env)
    env["native_controlled_protocol_only"] = True
    case = native.load_cases(BASELINE, "known_baseline")[0]
    original_async, original_sync = httpx.AsyncClient.send, httpx.Client.send
    wires: list[dict[str, Any]] = []

    def synchronous(client: httpx.Client, outgoing: httpx.Request, *args: Any, **options: Any) -> httpx.Response:
        if outgoing.url.host != "api.openai.com":
            return original_sync(client, outgoing, *args, **options)
        body = json.loads(outgoing.content)
        assert outgoing.url.path.endswith("/embeddings") and body["model"] == "text-embedding-3-small"
        wires.append({"kind": "embedding", "body": body})
        return httpx.Response(
            200,
            request=outgoing,
            json={"object": "list", "data": [{"object": "embedding", "index": 0, "embedding": [0.25] * 1536}], "model": body["model"], "usage": {"prompt_tokens": 17, "total_tokens": 17}},
        )

    async def asynchronous(client: httpx.AsyncClient, outgoing: httpx.Request, *args: Any, **options: Any) -> httpx.Response:
        if outgoing.url.host != "api.openai.com":
            return await original_async(client, outgoing, *args, **options)
        body = json.loads(outgoing.content)
        assert outgoing.url.path.endswith("/chat/completions") and body["model"] == "gpt-6.1-terra"
        wires.append({"kind": "chat", "body": body})
        usage = {"prompt_tokens": 61, "completion_tokens": 17, "total_tokens": 78, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 0}}
        if body.get("stream") is True:
            assert body["max_completion_tokens"] == 1024
            events = [
                {
                    "id": "native-controlled",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": body["model"],
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": case["user_input"]}, "finish_reason": None}],
                },
                {"id": "native-controlled", "object": "chat.completion.chunk", "created": 0, "model": body["model"], "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"id": "native-controlled", "object": "chat.completion.chunk", "created": 0, "model": body["model"], "choices": [], "usage": usage},
            ]
            raw = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events) + "data: [DONE]\n\n"
            return httpx.Response(200, request=outgoing, headers={"content-type": "text/event-stream"}, stream=SyntheticStream(raw.encode()))
        assert body["max_completion_tokens"] == 512
        text = "受控协议摘要保留否定和0/false/[]；这不是模型效果或实际人工结论。" if env["observer"].stage == "analysis" else "受控协议回答；这不是实际供应商回答或语义准确率。"
        return httpx.Response(
            200,
            request=outgoing,
            json={
                "id": "native-controlled",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": usage,
            },
        )

    def simulated_review(directory: Path, label: str, requested: dict[str, Any], journal: Any, **options: Any) -> dict[str, Any]:
        decision = {
            "request_sha256": compaction.digest(requested),
            "reviewed_by": "controlled_protocol_simulation_only",
            "synthetic_only": True,
            "judgments": {
                key: {"required": {row["id"]: "satisfied" for row in item["rubric"]["required"]}, "forbidden": {row["id"]: "satisfied" for row in item["rubric"]["forbidden"]}, "accepted": True}
                for key, item in requested["review_items"].items()
            },
            "notes": "Simulated protocol, not semantic quality, real supplier output or human acceptance.",
        }
        return compaction.validate_judgment(requested, decision)

    monkeypatch.setattr(httpx.AsyncClient, "send", asynchronous)
    monkeypatch.setattr(httpx.Client, "send", synchronous)
    monkeypatch.setattr(compaction, "external_review", simulated_review)
    directory = Path(request.config.getoption("--test-report-dir")) / f"controlled-mode-{case_index}"
    env["journal"].event("reserve-controlled-native-report-directory", path=str(directory))
    directory.mkdir(mode=0o700)
    report = native.run_case(env, monkeypatch, case, case_index, directory)
    assert report["mechanism_complete"] and len(wires) == len(env["observer"].attempts) == 5
    assert sum(row["body"].get("stream") is True for row in wires) == 1
    generation = next(row for row in env["observer"].attempts if row.get("native_provider_stream"))
    assert generation["provider_total_tokens"] == 78 and generation["finish_reason"] == "stop" and generation["raw_SSE_usage_receipt_count"] == 1
    assert report["withdrawal_stops_model"] is True
    env["journal"].event("controlled-native-LLM-complete", actual_external_calls=0, actual_SDK_calls_simulated=5, mode="stream" if case_index == 0 else "nonstream-template", review_simulated=True)
