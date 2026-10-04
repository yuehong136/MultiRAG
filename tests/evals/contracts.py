"""Deterministic scoring of independently specified quality expectations."""

import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

CORPUS = Path(__file__).parent / "corpus/v1"


def corpus_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(CORPUS.iterdir()):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


class QualityCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    query: str
    documents: list[str]
    facts: list[str]
    abstain: bool
    tools: list[str]
    tool_arguments: dict[str, str] = Field(default_factory=dict)


class Corpus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    provenance: str
    cases: list[QualityCase]

    @model_validator(mode="after")
    def unique_cases(self) -> "Corpus":
        if not self.cases or len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Corpus requires nonempty, unique case IDs")
        return self


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str = ""
    retrieved: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    abstained: bool = False
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    total_tokens: int = 0
    cost_usd: float | None = None
    latency_seconds: float = 0


def normalized(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def retrieval_scores(case: QualityCase, result: Observation, k: int = 3) -> dict[str, float]:
    expected = set(case.documents)
    if not expected:
        raise ValueError("Retrieval scoring requires a positive relevance judgement")
    ranked = list(dict.fromkeys(result.retrieved))[:k]
    first = next((index for index, item in enumerate(ranked, 1) if item in expected), None)
    return {"recall_at_3": len(expected.intersection(ranked)) / len(expected), "reciprocal_rank": 1 / first if first else 0.0}


def generation_assertions(case: QualityCase, result: Observation) -> dict[str, bool]:
    expected = set(case.documents)
    citations = set(result.citations)
    calls = [call.get("name") for call in result.tool_calls]
    return {
        "required_facts": all(
            bool(re.search(r"(?<!\d)" + re.escape(fact) + r"(?!\d)", normalized(result.answer))) if fact.isdigit() else normalized(fact) in normalized(result.answer) for fact in case.facts
        ),
        "abstention": result.abstained == case.abstain,
        "citation_precision": citations.issubset(expected.intersection(result.retrieved)) and (not expected or bool(citations)),
        "citation_coverage": expected.issubset(citations),
        "tool_selection": calls == case.tools,
        "tool_arguments": not case.tool_arguments or any(call.get("arguments") == case.tool_arguments for call in result.tool_calls),
    }
