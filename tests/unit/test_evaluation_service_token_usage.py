"""Synchronous evaluation accounting and failure contracts."""

import logging
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.db.services import dialog_service
from api.db.services.evaluation_service import EvaluationResultService, EvaluationService
from common.token_utils import encoder


@pytest.mark.parametrize("prompt", ["System instructions and retrieved context. Query: Capital?", None, ""])
def test_usage_counts_prompt_or_question(db: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, prompt: str | None) -> None:
    case = {"id": "case-1", "question": "Capital?"}
    dialog = SimpleNamespace()
    captured: list[dict[str, Any]] = []
    closed: list[bool] = []

    def chat(actual_dialog: Any, messages: list[dict[str, Any]], actual_db: Session, stream: bool = True) -> Generator[Any, None, None]:
        assert actual_dialog is dialog and actual_db is db and stream is False
        assert messages == [{"role": "user", "content": case["question"]}]
        try:
            yield "non-result"
            response: dict[str, Any] = {"answer": "Paris.", "reference": {"chunks": [{"chunk_id": "c1"}]}}
            if prompt is not None:
                response["prompt"] = prompt
            yield response
            pytest.fail("evaluation must not request another completion")
        finally:
            closed.append(True)

    def insert(actual_db: Session, **kwargs: Any) -> None:
        assert actual_db is db
        captured.append(kwargs)

    monkeypatch.setattr(dialog_service, "chat", chat)
    monkeypatch.setattr(EvaluationResultService, "insert", insert)
    with caplog.at_level(logging.DEBUG):
        result = EvaluationService._evaluate_single_case(db, "run-1", case, dialog)
    assert result is not None and captured == [result] and closed == [True]
    usage = result["token_usage"]
    expected_prompt = len(encoder.encode(prompt or case["question"]))
    expected_answer = len(encoder.encode("Paris."))
    assert usage == {
        "prompt_tokens": expected_prompt,
        "completion_tokens": expected_answer,
        "total_tokens": expected_prompt + expected_answer,
        "estimated": True,
        "tokenizer": "cl100k_base",
        "prompt_source": "chat_prompt" if prompt else "question_only",
    }
    assert result["retrieved_chunks"] == [{"chunk_id": "c1"}]
    if not prompt:
        assert "undercounts system and retrieved context" in caplog.text


@pytest.mark.parametrize("responses", [[], [None, "status"], [{"answer": None}], [{"answer": "", "prompt": ""}]])
def test_empty_output_preserves_zero(db: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, responses: list[Any]) -> None:
    captured: list[dict[str, Any]] = []

    def chat(*args: Any, **kwargs: Any) -> Generator[Any, None, None]:
        yield from responses

    def insert(actual_db: Session, **kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(dialog_service, "chat", chat)
    monkeypatch.setattr(EvaluationResultService, "insert", insert)
    result = EvaluationService._evaluate_single_case(db, "run-1", {"id": "case-1", "question": ""}, SimpleNamespace())
    assert result is not None and captured == [result]
    assert result["token_usage"]["prompt_tokens"] == 0
    assert result["token_usage"]["completion_tokens"] == 0
    assert result["token_usage"]["total_tokens"] == 0
    if not any(isinstance(item, dict) for item in responses):
        assert "produced no answer from chat" in caplog.text


@pytest.mark.parametrize("failure", ["chat", "tokenizer", "sql"])
def test_failed_case_does_not_return_success(db: Session, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    attempted: list[dict[str, Any]] = []
    closed: list[bool] = []

    def chat(*args: Any, **kwargs: Any) -> Generator[dict[str, Any], None, None]:
        try:
            if failure == "chat":
                raise RuntimeError("provider unavailable")
            yield {"answer": "Paris.", "prompt": "Capital?"}
        finally:
            closed.append(True)

    def encode(*args: Any, **kwargs: Any) -> list[int]:
        raise RuntimeError("tokenizer unavailable")

    def insert(actual_db: Session, **kwargs: Any) -> None:
        attempted.append(kwargs)
        raise RuntimeError("write failed")

    monkeypatch.setattr(dialog_service, "chat", chat)
    monkeypatch.setattr(EvaluationResultService, "insert", insert)
    if failure == "tokenizer":
        monkeypatch.setattr(encoder, "encode", encode)
    assert EvaluationService._evaluate_single_case(db, "run-1", {"id": "case-1", "question": "Capital?"}, SimpleNamespace()) is None
    assert bool(attempted) is (failure == "sql")
    assert closed == [True]


def test_literal_special_token_is_counted_as_text(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def chat(*args: Any, **kwargs: Any) -> Generator[dict[str, Any], None, None]:
        yield {"answer": "<|endoftext|>", "prompt": "<|endoftext|>"}

    monkeypatch.setattr(dialog_service, "chat", chat)
    monkeypatch.setattr(EvaluationResultService, "insert", lambda *args, **kwargs: None)
    result = EvaluationService._evaluate_single_case(db, "run", {"id": "case", "question": "?"}, SimpleNamespace())
    assert result is not None and result["token_usage"]["prompt_tokens"] > 0 and result["token_usage"]["completion_tokens"] > 0


def test_summary_keeps_metric_averages() -> None:
    assert EvaluationService._compute_summary_metrics([]) == {}
    summary = EvaluationService._compute_summary_metrics(
        [
            {"execution_time": 1.0, "metrics": {"precision": 0.0, "answer_length": 10}},
            {"execution_time": 3.0, "metrics": {"precision": 1.0, "answer_length": 20}},
        ]
    )
    assert summary == {"total_cases": 2, "avg_execution_time": 2.0, "avg_precision": 0.5, "avg_answer_length": 15.0}
