"""Opt-in real provider evaluation through MultiRAG's chat/tool adapter."""

import json
import os
import re
import time
from typing import Any

import pytest

from scripts.provision_eval_assets import MODEL, REVISION
from tests.evals.contracts import CORPUS, Corpus, Observation, QualityCase, corpus_fingerprint, generation_assertions
from tests.evals.runtime import _report_path
from tests.evals.runtime import quality_runtime as quality_runtime

PROMPT_VERSION = "grounded-json-v1"
SYSTEM = """仅依据给出的资料或工具结果回答问题。资料不足时明确拒绝猜测。
引用只允许使用资料的文件名。工单状态必须调用 ticket_status；其他问题不要调用工具。
最终输出一个 JSON 对象，字段 answer 为中文答案，citations 为引用文件名列表，abstained 为布尔值。
不要输出 Markdown 代码围栏。"""
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ticket_status",
            "description": "查询指定工单的当前状态",
            "parameters": {"type": "object", "properties": {"ticket_id": {"type": "string"}}, "required": ["ticket_id"], "additionalProperties": False},
        },
    }
]


@pytest.fixture
def generation_runtime(request: pytest.FixtureRequest) -> dict[str, Any]:
    required = ["MULTIRAG_EVAL_BASE_URL", "MULTIRAG_EVAL_API_KEY", "MULTIRAG_EVAL_MODEL"]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        _report_path(request, "quality-generation.json").write_text(json.dumps({"status": "blocked", "missing_environment": missing, "executed_cases": 0}, indent=2))
        pytest.fail("Real-provider evaluation requires " + ", ".join(missing), pytrace=False)
    return request.getfixturevalue("quality_runtime")


def test_real_provider_answers_citations_abstention_and_tools(generation_runtime: dict[str, Any], request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import TypeAdapter
    from pydantic_evals import Case, Dataset
    from pydantic_evals.evaluators import Evaluator, EvaluatorContext

    from core.llm.chat import OpenAI_APIChat

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "45")
    env = generation_runtime
    corpus = Corpus.model_validate_json((CORPUS / "cases.json").read_text())

    class Grounding(Evaluator[QualityCase, Observation, Any]):
        def evaluate(self, ctx: EvaluatorContext[QualityCase, Observation, Any]) -> dict[str, bool]:
            return generation_assertions(ctx.inputs, ctx.output)

    async def candidate(case: QualityCase) -> Observation:
        started = time.monotonic()
        result = await env["dealer"].retrieval(
            case.query,
            None,
            env["model"],
            env["owner"],
            [env["name"]],
            1,
            3,
            similarity_threshold=0,
            vector_similarity_weight=1,
            top=20,
            aggs=False,
            search_mode={"dense": {}},
            kb_ids=[env["dataset"]],
        )
        retrieved = list(dict.fromkeys(row["docnm_kwd"] for row in result["chunks"]))
        calls: list[dict[str, Any]] = []

        class ToolSession:
            async def tool_call_async(self, name: str, arguments: dict[str, Any]) -> str:
                calls.append({"name": name, "arguments": arguments})
                # This owned, read-only service has one explicitly versioned record.
                return json.dumps({"status": "处理中"} if name == "ticket_status" and arguments == {"ticket_id": "QH-2048"} else {"error": "not_found"}, ensure_ascii=False)

        model = OpenAI_APIChat(os.environ["MULTIRAG_EVAL_API_KEY"], os.environ["MULTIRAG_EVAL_MODEL"], os.environ["MULTIRAG_EVAL_BASE_URL"], max_retries=0, max_rounds=2)
        model.client = model.client.with_options(max_retries=0)
        model.async_client = model.async_client.with_options(max_retries=0)
        model.bind_tools(ToolSession(), TOOLS)
        context = "\n\n".join(f"[{row['docnm_kwd']}]\n{row['content_with_weight']}" for row in result["chunks"])
        try:
            answer, total_tokens = await model.async_chat_with_tools(SYSTEM, [{"role": "user", "content": f"资料：\n{context}\n\n问题：{case.query}"}], {"temperature": 0, "max_tokens": 512})
        finally:
            await model.async_client.close()
            model.client.close()
        # The production adapter can prefix diagnostic tool/reasoning blocks.
        clean = re.sub(r"<think>.*?</think>|<tool_call>.*?</tool_call>", "", answer, flags=re.DOTALL).strip()
        payload = json.loads(clean)
        return Observation(
            answer=payload["answer"],
            citations=payload["citations"],
            abstained=payload["abstained"],
            retrieved=retrieved,
            tool_calls=calls,
            total_tokens=total_tokens,
            latency_seconds=time.monotonic() - started,
        )

    dataset = Dataset(name="multirag-grounded-generation-v1", cases=[Case(name=case.id, inputs=case) for case in corpus.cases], evaluators=[Grounding()])
    report = dataset.evaluate_sync(
        candidate,
        max_concurrency=1,
        progress=False,
        metadata={
            "corpus_version": corpus.version,
            "corpus_sha256": corpus_fingerprint(),
            "embedding_model": MODEL,
            "embedding_revision": REVISION,
            "model": os.environ["MULTIRAG_EVAL_MODEL"],
            "prompt_version": PROMPT_VERSION,
            "cost_usd": None,
            "cost_note": "Provider returns total tokens; currency cost requires provider billing, never guessed.",
        },
    )
    _report_path(request, "quality-generation.json").write_bytes(TypeAdapter(type(report)).dump_json(report, indent=2))
    assert not report.failures, [failure.name for failure in report.failures]
    assert len(report.cases) == len(dataset.cases)
    assert all(not case.evaluator_failures and case.assertions for case in report.cases)
    assert all(assertion.value for case in report.cases for assertion in case.assertions.values()), "See quality-generation.json"
