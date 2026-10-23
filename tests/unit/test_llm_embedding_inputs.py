"""Embedding input normalization preserves provider results and bundle boundaries."""

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
from sqlalchemy.orm import Session

from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import TenantLangfuseService, TenantLLMService


class _Embedding:
    def __init__(self) -> None:
        self.inputs: list[list[str] | str] = []
        self.tokens = 13
        self.error: Exception | None = None
        self.vectors = np.array([[0.25, 0.75]])

    def encode(self, texts: list[str]) -> tuple[np.ndarray, int]:
        self.inputs.append(texts)
        if self.error:
            raise self.error
        return self.vectors, self.tokens

    def encode_queries(self, query: str) -> tuple[np.ndarray, int]:
        self.inputs.append(query)
        if self.error:
            raise self.error
        return self.vectors[0], self.tokens


@pytest.fixture
def make_bundle(monkeypatch: pytest.MonkeyPatch, db: Session) -> Callable[..., tuple[LLMBundle, _Embedding, list[tuple[int, int]]]]:
    def build(*, trace: bool = False) -> tuple[LLMBundle, _Embedding, list[tuple[int, int]]]:
        model = _Embedding()
        accounting: list[tuple[int, int]] = []
        monkeypatch.setattr(TenantLLMService, "model_instance", classmethod(lambda cls, *args, **kwargs: model))
        monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", classmethod(lambda cls, *args, **kwargs: None))
        monkeypatch.setattr(TenantLLMService, "increase_usage_by_id", lambda model_id, tokens: accounting.append((model_id, tokens)) or 1)
        bundle = LLMBundle(db, "embedding-input-tenant", {"id": 71, "llm_name": "embedding-model", "llm_factory": "OpenAI", "mdl_type": "embedding", "max_tokens": 100})
        if trace:
            bundle.langfuse = MagicMock()
            bundle.trace_context = {"trace_id": "embedding-input-trace"}
        return bundle, model, accounting

    return build


@pytest.mark.parametrize("trace", [False, True])
def test_batch_replaces_blanks_without_dropping_or_mutating_inputs(make_bundle: Callable[..., Any], caplog: pytest.LogCaptureFixture, trace: bool) -> None:
    bundle, model, accounting = make_bundle(trace=trace)
    texts = ["valid", "", " \n\t", None, "\u2003", "  keep spaces  "]
    original = texts.copy()
    model.vectors = np.arange(1, 19).reshape(6, 3)

    vectors, tokens = bundle.encode(texts)

    assert texts == original
    assert model.inputs == [["valid", "None", "None", "None", "None", "  keep spaces  "]]
    assert vectors is model.vectors
    assert vectors.shape == (6, 3)
    assert tokens == 13
    assert accounting == [(71, 13)]
    assert len(caplog.records) == 4
    for index in (1, 2, 3, 4):
        assert f"index {index}" in caplog.text
    assert "embedding-model" in caplog.text
    if trace:
        bundle.langfuse.start_observation.assert_called_once_with(as_type="generation", trace_context=bundle.trace_context, name="encode", model="embedding-model", input={"texts": original})
        generation = bundle.langfuse.start_observation.return_value
        generation.update.assert_called_once_with(usage_details={"total_tokens": 13})
        generation.end.assert_called_once_with()


@pytest.mark.parametrize("query", [None, "", " \n\t", "\u2003", "  valid query  "])
def test_queries_replace_only_blank_inputs(make_bundle: Callable[..., Any], query: str | None) -> None:
    bundle, model, accounting = make_bundle(trace=True)

    vectors, tokens = bundle.encode_queries(query)

    assert model.inputs == ["  valid query  " if query == "  valid query  " else "None"]
    np.testing.assert_array_equal(vectors, model.vectors[0])
    assert vectors.shape == (2,)
    assert tokens == 13
    assert accounting == [(71, 13)]
    bundle.langfuse.start_observation.assert_called_once_with(as_type="generation", trace_context=bundle.trace_context, name="encode_queries", model="embedding-model", input={"query": query})
    generation = bundle.langfuse.start_observation.return_value
    generation.update.assert_called_once_with(usage_details={"total_tokens": 13})
    generation.end.assert_called_once_with()


def test_empty_batch_preserves_provider_result_and_zero_usage(make_bundle: Callable[..., Any]) -> None:
    bundle, model, accounting = make_bundle(trace=True)
    model.vectors = np.array([])
    model.tokens = 0

    vectors, tokens = bundle.encode([])

    assert model.inputs == [[]]
    assert vectors is model.vectors
    assert tokens == 0
    assert accounting == []
    generation = bundle.langfuse.start_observation.return_value
    generation.update.assert_called_once_with(usage_details={"total_tokens": 0})
    generation.end.assert_called_once_with()


@pytest.mark.parametrize("query", [False, True])
def test_zero_provider_usage_remains_valid(make_bundle: Callable[..., Any], query: bool) -> None:
    bundle, model, accounting = make_bundle(trace=True)
    model.tokens = 0

    vectors, tokens = bundle.encode_queries(None) if query else bundle.encode([None])

    assert vectors.size > 0
    assert tokens == 0
    assert accounting == []
    generation = bundle.langfuse.start_observation.return_value
    generation.update.assert_called_once_with(usage_details={"total_tokens": 0})
    generation.end.assert_called_once_with()


@pytest.mark.parametrize("query", [False, True])
def test_provider_error_propagates_without_accounting_or_success_trace(make_bundle: Callable[..., Any], query: bool) -> None:
    bundle, model, accounting = make_bundle(trace=True)
    error = RuntimeError("controlled embedding rejection")
    model.error = error

    with pytest.raises(RuntimeError) as raised:
        bundle.encode_queries(None) if query else bundle.encode([None])

    assert raised.value is error
    assert model.inputs == ["None" if query else ["None"]]
    assert accounting == []
    bundle.langfuse.start_observation.return_value.update.assert_not_called()


@pytest.mark.parametrize("token_count", [95, 96, 101])
def test_batch_keeps_the_truncation_safety_margin(make_bundle: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, token_count: int) -> None:
    bundle, model, _ = make_bundle()
    text = "long text " * 20
    monkeypatch.setattr("api.db.services.llm_service.num_tokens_from_string", lambda value: token_count)

    bundle.encode([text])

    assert model.inputs == [[text if token_count == 95 else text[:95]]]


@pytest.mark.parametrize("query", [False, True])
@pytest.mark.parametrize("pending_write", [False, True])
def test_db_release_precedes_provider_without_touching_pending_writes(make_bundle: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, query: bool, pending_write: bool) -> None:
    bundle, model, _ = make_bundle()
    caller = MagicMock(spec=Session)
    caller.in_transaction.return_value = True
    caller.dirty = {object()} if pending_write else set()
    caller.new = set()
    caller.deleted = set()
    bundle.db = caller
    events: list[str] = []
    caller.rollback.side_effect = lambda: events.append("release")

    def encode(value: list[str] | str) -> tuple[np.ndarray, int]:
        events.append("provider")
        return model.vectors, 0

    monkeypatch.setattr(model, "encode_queries" if query else "encode", encode)

    bundle.encode_queries(None) if query else bundle.encode([None])

    assert events == (["provider"] if pending_write else ["release", "provider"])
    caller.commit.assert_not_called()
    assert caller.rollback.call_count == (0 if pending_write else 1)
