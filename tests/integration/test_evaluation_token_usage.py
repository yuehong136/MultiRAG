"""Evaluation JSONB commits, failure side effects, and run-result readback."""

from collections.abc import Generator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import EvaluationCase, EvaluationResult, EvaluationRun
from api.db.services import dialog_service
from api.db.services.evaluation_service import EvaluationService
from common.token_utils import encoder


def test_usage_commits_and_summary_reads_back(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = bootstrapped_engine
    run_id, dataset_id = uuid4().hex, uuid4().hex
    cases = [uuid4().hex, uuid4().hex]

    def chat(dialog: Any, messages: list[dict[str, Any]], db: Session, stream: bool = True) -> Generator[dict[str, Any], None, None]:
        assert isinstance(db, Session) and stream is False
        if messages[0]["content"]:
            yield {"answer": "Paris.", "prompt": "System context. Capital?", "reference": {"chunks": [{"chunk_id": "c1"}]}}
        # The empty-question case deliberately yields nothing.

    monkeypatch.setattr(dialog_service, "chat", chat)
    with Session(engine) as writer:
        writer.add(EvaluationRun(id=run_id, dataset_id=dataset_id, dialog_id=uuid4().hex, name="usage", config_snapshot={}, created_by="tester", run_status="RUNNING"))
        writer.add_all([EvaluationCase(id=cases[0], dataset_id=dataset_id, question="Capital?", relevant_chunk_ids=["c1"]), EvaluationCase(id=cases[1], dataset_id=dataset_id, question="")])
        writer.commit()
        EvaluationService._execute_evaluation(writer, run_id, dataset_id, SimpleNamespace())
    try:
        with Session(engine) as reader:
            # New connection/session proves committed JSONB rather than cached ORM state.
            rows = reader.scalars(sa.select(EvaluationResult).where(EvaluationResult.run_id == run_id)).all()
            assert len(rows) == 2
            by_case = {row.case_id: row for row in rows}
            usage = by_case[cases[0]].to_dict()["token_usage"]
            assert usage == {
                "prompt_tokens": len(encoder.encode("System context. Capital?")),
                "completion_tokens": len(encoder.encode("Paris.")),
                "total_tokens": len(encoder.encode("System context. Capital?")) + len(encoder.encode("Paris.")),
                "estimated": True,
                "tokenizer": "cl100k_base",
                "prompt_source": "chat_prompt",
            }
            empty_usage = by_case[cases[1]].token_usage
            assert empty_usage["total_tokens"] == empty_usage["prompt_tokens"] == empty_usage["completion_tokens"] == 0
            assert empty_usage["prompt_source"] == "question_only"
            result = EvaluationService.get_run_results(reader, run_id)
            assert len(result["results"]) == 2
            assert result["run"]["run_status"] == "COMPLETED"
            summary = result["run"]["metrics_summary"]
            assert summary["total_cases"] == 2 and summary["avg_precision"] == 1.0
            assert summary["avg_answer_length"] == len("Paris.")
            assert summary["avg_execution_time"] >= 0
    finally:
        with Session(engine) as cleaner:
            cleaner.execute(sa.delete(EvaluationResult).where(EvaluationResult.run_id == run_id))
            cleaner.execute(sa.delete(EvaluationRun).where(EvaluationRun.id == run_id))
            cleaner.execute(sa.delete(EvaluationCase).where(EvaluationCase.dataset_id == dataset_id))
            cleaner.commit()


def test_provider_failure_writes_no_result(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid4().hex

    def chat(*args: Any, **kwargs: Any) -> Generator[dict[str, Any], None, None]:
        raise RuntimeError("controlled provider failure")
        yield {}  # pragma: no cover

    monkeypatch.setattr(dialog_service, "chat", chat)
    with Session(bootstrapped_engine) as writer:
        assert EvaluationService._evaluate_single_case(writer, run_id, {"id": uuid4().hex, "question": "Capital?"}, SimpleNamespace()) is None
    with Session(bootstrapped_engine) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(EvaluationResult).where(EvaluationResult.run_id == run_id)) == 0


def test_sql_rejection_writes_no_result(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid4().hex

    def chat(*args: Any, **kwargs: Any) -> Generator[dict[str, Any], None, None]:
        yield {"answer": "Paris.", "prompt": "Capital?"}

    monkeypatch.setattr(dialog_service, "chat", chat)
    with Session(bootstrapped_engine) as writer:
        # Existing NOT NULL SQL contract rejects a result with no case_id.
        assert EvaluationService._evaluate_single_case(writer, run_id, {"id": None, "question": "Capital?"}, SimpleNamespace()) is None
        writer.rollback()
    with Session(bootstrapped_engine) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(EvaluationResult).where(EvaluationResult.run_id == run_id)) == 0
