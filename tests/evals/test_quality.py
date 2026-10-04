"""Real local parsing, OCR and neural retrieval with versioned Pydantic Evals reports."""

import importlib.metadata
import json
import time
from typing import Any

import numpy as np
import pytest
from PIL import Image

from scripts.provision_eval_assets import MODEL, REVISION
from scripts.provision_test_assets import REVISION as OCR_REVISION
from tests.evals.contracts import CORPUS, Corpus, Observation, QualityCase, corpus_fingerprint, retrieval_scores
from tests.evals.runtime import _report_path
from tests.evals.runtime import quality_runtime as quality_runtime


def test_local_retrieval_quality(quality_runtime: dict[str, Any], request: pytest.FixtureRequest) -> None:
    from pydantic import TypeAdapter
    from pydantic_evals import Case, Dataset
    from pydantic_evals.evaluators import Evaluator, EvaluatorContext

    corpus = Corpus.model_validate_json((CORPUS / "cases.json").read_text())
    env = quality_runtime

    class Relevance(Evaluator[QualityCase, Observation, Any]):
        def evaluate(self, ctx: EvaluatorContext[QualityCase, Observation, Any]) -> dict[str, float | bool]:
            scores = retrieval_scores(ctx.inputs, ctx.output)
            return {**scores, "all_relevant_in_top3": scores["recall_at_3"] == 1}

    async def retrieve(case: QualityCase) -> Observation:
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
        return Observation(retrieved=[row["docnm_kwd"] for row in result["chunks"]], latency_seconds=time.monotonic() - started, cost_usd=0)

    dataset = Dataset(name="multirag-chinese-retrieval-v1", cases=[Case(name=case.id, inputs=case) for case in corpus.cases if case.documents], evaluators=[Relevance()])
    metadata = {
        "corpus_version": corpus.version,
        "corpus_sha256": corpus_fingerprint(),
        "model": MODEL,
        "model_revision": REVISION,
        "prompt_version": None,
        "pydantic_evals": importlib.metadata.version("pydantic-evals"),
        "scope": "production-parser-embedding-and-retrieval",
        "provider_cost_usd": 0,
    }
    report = dataset.evaluate_sync(retrieve, name=dataset.name, max_concurrency=1, progress=False, metadata=metadata)
    _report_path(request, "quality-retrieval.json").write_bytes(TypeAdapter(type(report)).dump_json(report, indent=2))
    assert not report.failures, [failure.name for failure in report.failures]
    assert len(report.cases) == len(dataset.cases)
    assert all(not case.evaluator_failures and case.assertions for case in report.cases)
    assert all(assertion.value for case in report.cases for assertion in case.assertions.values()), "See quality-retrieval.json for relevance regressions"


def test_real_parser_and_ocr_quality(quality_runtime: dict[str, Any], request: pytest.FixtureRequest) -> None:
    from deepdoc.vision.ocr import OCR

    started = time.monotonic()
    text = {name: "\n".join(chunk["content_with_weight"] for chunk in chunks) for name, chunks in quality_runtime["parsed"].items()}
    findings = {
        "chinese_text": all(word in text["purchase.md"] for word in ["5000", "20000", "财务负责人"]),
        "table_cells": all(word in text["plans.md"] for word in ["团队版", "512", "240", "研究版", "2048", "960"]),
    }
    ocr = OCR()
    lines = ocr(np.asarray(Image.open(CORPUS / "receipt.png").convert("RGB")))
    recognized = " ".join(line[1][0] for line in lines)
    findings["ocr_identifiers"] = all(value in recognized for value in ["QH-2048", "12", "2026-09-18"])
    findings["ocr_chinese"] = all(value in recognized for value in ["青禾研究院", "设备入库单", "设备编号", "数量", "验收日期"])
    _report_path(request, "quality-parsing.json").write_text(
        json.dumps(
            {"findings": findings, "recognized": recognized, "seconds": time.monotonic() - started, "ocr_revision": OCR_REVISION, "corpus_sha256": corpus_fingerprint()}, ensure_ascii=False, indent=2
        )
    )
    assert all(findings.values()), findings
