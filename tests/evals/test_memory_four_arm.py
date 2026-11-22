"""Explicit four-arm evaluation using owned PostgreSQL, Milvus and Valkey."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support import memory_workload_runtime
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_four_arm import MAX_ANSWERS, PHASES, load_cases, run_case
from tests.support.memory_structured_sources_live import require_direct_openai_models
from tests.support.memory_workload_runtime import memory_live_api as memory_live_api

KNOWN = Path(__file__).parents[1] / "fixtures/memory_four_arm_known.json"
CASES = [("four_arm_known", item) for item in load_cases(KNOWN, "four_arm_known")]
if sealed := os.environ.get("MULTIRAG_FOUR_ARM_HOLDOUT"):
    CASES += [("four_arm_holdout", item) for item in load_cases(Path(sealed), "four_arm_holdout", expected_sha256=os.environ["MULTIRAG_FOUR_ARM_HOLDOUT_SHA256"])]
assert len({item["id"] for _, item in CASES}) == len(CASES)


def controlled_models(identifiers: dict[str, int]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "id": identifier,
            "llm_name": "gpt-6.1-terra" if key == "chat" else "text-embedding-3-small",
            "llm_factory": "OpenAI",
            "api_key": "owned-synthetic-unused",
            "api_base": "https://api.openai.com/v1",
            "max_tokens": 8192,
        }
        for key, identifier in identifiers.items()
    }


def controlled_sdk(patch: pytest.MonkeyPatch, case: dict[str, Any]) -> list[dict[str, Any]]:
    sends: list[dict[str, Any]] = []
    original_sync, original_async = httpx.Client.send, httpx.AsyncClient.send

    def reply(request: httpx.Request) -> httpx.Response | None:
        if request.url.host != "api.openai.com":
            return None
        assert request.method == "POST"
        body = json.loads(request.content)
        sends.append(body)
        model = body["model"]
        if request.url.path.endswith("/embeddings"):
            assert model == "text-embedding-3-small"
            result = {
                "object": "list",
                "model": model,
                "data": [{"object": "embedding", "index": index, "embedding": [1.0, 0.0]} for index in range(len(body["input"]))],
                "usage": {"prompt_tokens": 8, "total_tokens": 8},
            }
            return httpx.Response(200, json=result, request=request)
        assert request.url.path.endswith("/chat/completions") and model == "gpt-6.1-terra"
        assert body["max_completion_tokens"] in (512, 1024)
        messages = body["messages"]
        first, last = messages[0]["content"], messages[-1]["content"]
        if "never publish" in first:
            analysis = json.loads(last)
            inputs = [item["input_id"] for item in analysis["inputs"]]
            assert len(inputs) == 2
            corrected = case["correction"] in last
            text = json.dumps(
                {
                    "proposals": [
                        {
                            "action": "add",
                            "content": case["replacement"] if corrected else "；".join(case["initial"]),
                            "message_type": "semantic",
                            "source_input_ids": inputs,
                            "reason": "Combine two controlled synthetic sources.",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        elif last in case["questions"].values():
            text = "受控答复仅用于 HTTP、SDK 和停供合同检查。"
        else:
            matched = [source for source in [*case["initial"], case["correction"]] if source in last]
            assert len(matched) == 1
            text = json.dumps({"semantic": [{"content": matched[0], "valid_at": "2026-11-15 09:00:00", "invalid_at": None}]}, ensure_ascii=False)
        result = {
            "id": "chatcmpl-synthetic-four-arm",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 64, "completion_tokens": 16, "total_tokens": 80, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 0}},
        }
        return httpx.Response(200, json=result, request=request)

    def sync(client: httpx.Client, request: httpx.Request, **options: Any) -> httpx.Response:
        response = reply(request)
        return response if response is not None else original_sync(client, request, **options)

    async def asynchronous(client: httpx.AsyncClient, request: httpx.Request, **options: Any) -> httpx.Response:
        response = reply(request)
        return response if response is not None else await original_async(client, request, **options)

    patch.setattr(httpx.Client, "send", sync)
    patch.setattr(httpx.AsyncClient, "send", asynchronous)
    return sends


def test_controlled_four_arm(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    sample = deepcopy(CASES[0][1])
    sample["rubric"] = {phase: ["EVAL_ONLY_REVIEW_CRITERION:" + phase] for phase in PHASES}
    monkeypatch.setenv("MULTIRAG_MEMORY_LIVE", "1")
    monkeypatch.setenv("MULTIRAG_MEMORY_LIVE_PROVIDER", "OpenAI")
    monkeypatch.setenv("MULTIRAG_MEMORY_CHAT_MODEL_ID", "1")
    monkeypatch.setenv("MULTIRAG_MEMORY_EMBEDDING_MODEL_ID", "2")
    monkeypatch.setattr(memory_workload_runtime, "configured_models", controlled_models)
    sends = controlled_sdk(monkeypatch, sample)
    env = request.getfixturevalue("memory_live_api")
    report = run_case(env, monkeypatch, sample, Path(request.config.getoption("--test-report-dir")), "controlled", controlled=True)
    assert report["mechanism_complete"] and len(report["answers"]) == MAX_ANSWERS
    assert all(len(row["derived"]) == 1 for records in report["source_records"].values() for row in records)
    assert all(report["revisions"][arm]["state"] == "revised" for arm in ("B", "C"))
    assert all(report[phase]["selected_count"] == 1 for phase in PHASES[:2])
    assert [len(row["startup"]["items"]) for row in report["answers"] if row["arm"] == "D"] == [1, 1, 0]
    assert len(sends) == len(report["cost"]["workload"]["observed_attempts"])
    chat = [body for body in sends if "messages" in body]
    assert len(chat) == 9 + 2 + MAX_ANSWERS
    assert all(not any(criterion in json.dumps(body, ensure_ascii=False) for criteria in sample["rubric"].values() for criterion in criteria) for body in chat)


@pytest.mark.parametrize(("kind", "case"), CASES, ids=[item["id"] for _, item in CASES])
def test_live_four_arm(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, kind: str, case: dict[str, Any]) -> None:
    assert len(CASES) == 4, "freeze two known and two sealed cases before paid evaluation"
    assert os.environ.get("MULTIRAG_RUN_ID") and os.environ.get("MULTIRAG_MEMORY_LIVE_PROVIDER") == "OpenAI"
    bindings = Path(os.environ["MULTIRAG_FOUR_ARM_PROVIDER_BINDINGS"])
    assert bindings.resolve().is_relative_to(Path(os.environ["TMPDIR"]).resolve()) and not bindings.is_symlink()
    assert bindings.stat().st_uid == os.getuid() and bindings.stat().st_mode & 0o777 == 0o600

    def models(identifiers: dict[str, int]) -> dict[str, dict[str, Any]]:
        copied = json.loads(bindings.read_text())
        assert set(copied) == set(identifiers)
        return copied

    monkeypatch.setattr(memory_workload_runtime, "configured_models", models)
    env = request.getfixturevalue("memory_live_api")
    require_direct_openai_models(env)
    report = run_case(env, monkeypatch, case, Path(request.config.getoption("--test-report-dir")), kind, controlled=False)
    assert report["mechanism_complete"] and len(report["answers"]) == MAX_ANSWERS
