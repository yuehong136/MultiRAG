"""Batch contracts and measured copied elements on the production entrypoints."""

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest
from sqlalchemy.orm import Session

from common.token_utils import num_tokens_from_string
from core.flow.tokenizer import tokenizer as tokenizer_module
from core.llm.embedding import BuiltinEmbed, DefaultEmbedding
from core.llm.embedding_model.default_embedding import DefaultEmbedding as LegacyDefaultEmbedding
from core.svr import task_executor


class Encoder:
    max_length = 128

    def __init__(self, dtype: str, dim: int = 3) -> None:
        self.dtype = dtype
        self.dim = dim
        self.calls: list[list[str]] = []
        self.outputs: list[np.ndarray] = []

    def encode(self, texts: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(texts))
        vectors = np.array([[float(t) if t.isdigit() else 100.0] * self.dim for t in texts], dtype=self.dtype).reshape(len(texts), self.dim)
        self.outputs.append(vectors)
        return vectors if kwargs.get("convert_to_numpy") else (vectors, len(texts) * 3)


def track_copies(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    original = np.concatenate
    copies: list[tuple[int, str]] = []

    def concatenate(arrays: Any, *args: Any, **kwargs: Any) -> np.ndarray:
        result = original(arrays, *args, **kwargs)
        copies.append((sum(a.size for a in arrays), str(result.dtype)))
        return result

    monkeypatch.setattr(np, "concatenate", concatenate)
    return copies


@pytest.mark.parametrize("model_class", [BuiltinEmbed, DefaultEmbedding, LegacyDefaultEmbedding])
@pytest.mark.parametrize("size", [0, 1, 16, 17, 65])
@pytest.mark.parametrize("dtype", ["float32", "float64"])
@pytest.mark.parametrize("dim", [1, 3])
def test_model_batches_preserve_values_shape_dtype_tokens_and_empty(monkeypatch: pytest.MonkeyPatch, model_class: type, size: int, dtype: str, dim: int) -> None:
    model = model_class.__new__(model_class)
    encoder = Encoder(dtype, dim)
    model._model = encoder
    texts = [str(i) for i in range(size)]
    copies = track_copies(monkeypatch)
    actual, tokens = model.encode(texts)
    assert encoder.calls == [texts[i : i + 16] for i in range(0, size, 16)]
    assert tokens == (size * 3 if model_class is BuiltinEmbed else sum(num_tokens_from_string(t) for t in texts))
    if not size:
        assert actual is None
    else:
        assert actual.shape == (size, dim)
        assert actual.dtype == np.dtype(dtype)
        np.testing.assert_array_equal(actual, np.repeat(np.arange(size, dtype=dtype)[:, None], dim, axis=1))
        if size <= 16:
            assert actual is encoder.outputs[0]
    assert copies == ([(size * dim, dtype)] if size > 16 else [])


@pytest.mark.parametrize("size", [0, 1, 2, 5])
@pytest.mark.parametrize("dtype", ["float32", "float64"])
@pytest.mark.parametrize("dim", [1, 3])
async def test_tokenizer_preserves_existing_title_layout_and_chunk_order(monkeypatch: pytest.MonkeyPatch, db: Session, size: int, dtype: str, dim: int) -> None:
    encoder = Encoder(dtype, dim)
    monkeypatch.setattr(tokenizer_module, "db_connection", lambda: nullcontext(db))
    monkeypatch.setattr(tokenizer_module, "get_tenant_default_model_by_type", lambda *a: {})
    monkeypatch.setattr(tokenizer_module, "LLMBundle", lambda *a: encoder)
    monkeypatch.setattr(tokenizer_module.settings, "EMBEDDING_BATCH_SIZE", 2)
    tokenizer = tokenizer_module.Tokenizer.__new__(tokenizer_module.Tokenizer)
    tokenizer._param = tokenizer_module.TokenizerParam()
    tokenizer._canvas = SimpleNamespace(_tenant_id="tenant", _kb_id=None)
    tokenizer.callback = lambda *a, **k: None
    chunks = [{"text": str(i)} for i in range(size)] + [{"text": "  "}]
    copies = track_copies(monkeypatch)
    actual, tokens = await tokenizer._embedding("title", chunks)
    assert actual is chunks
    assert tokens == (size + 1) * 3 if size else tokens == 0
    assert "q_3_vec" not in chunks[-1] and "q_1_vec" not in chunks[-1]
    for i, chunk in enumerate(chunks[:-1]):
        # Existing flattened titles blend only for dimension 1, where NumPy
        # broadcasts to (size, size). Dimension >1 previously bypassed blending.
        expected = [10 + 0.9 * i] * size if dim == 1 else [i] * dim
        np.testing.assert_allclose(chunk[f"q_{len(expected)}_vec"], expected)
    assert copies == ([(size * dim, dtype)] if size > 2 else [])
    assert encoder.calls == ([["title"]] + [[str(i) for i in range(j, min(size, j + 2))] for j in range(0, size, 2)] if size else [])


@pytest.mark.parametrize("size", [0, 1, 2, 5])
@pytest.mark.parametrize("dtype", ["float32", "float64"])
async def test_classic_task_batch_order_weighting_and_copies(monkeypatch: pytest.MonkeyPatch, size: int, dtype: str) -> None:
    encoder = Encoder(dtype)
    monkeypatch.setattr(task_executor.settings, "EMBEDDING_BATCH_SIZE", 2)
    docs = [{"docnm_kwd": "title", "content_with_weight": str(i)} for i in range(size)]
    progress: list[dict[str, Any]] = []
    copies = track_copies(monkeypatch)
    tokens = await task_executor.embedding(docs, encoder, {"filename_embd_weight": 0.25}, lambda **k: progress.append(k))
    assert tokens == (size + 1) * 3 if size else tokens == 0
    assert len(progress) == (size + 1) // 2
    for i, doc in enumerate(docs):
        np.testing.assert_allclose(doc["vector"], [25 + 0.75 * i] * 3)
        assert doc["vector"] == doc["q_3_vec"]
    assert copies == ([(size * 3, dtype)] if size > 2 else [])
    if not size:
        assert encoder.calls == []


@pytest.mark.parametrize("size", [0, 1, 2, 5])
@pytest.mark.parametrize("dtype", ["float32", "float64"])
async def test_dataflow_batches_reach_index_in_order_with_token_total(monkeypatch: pytest.MonkeyPatch, db: Session, size: int, dtype: str) -> None:
    from api.db.services.canvas_service import UserCanvasService
    from core.flow.pipeline import Pipeline

    encoder = Encoder(dtype)
    monkeypatch.setattr(task_executor.settings, "EMBEDDING_BATCH_SIZE", 2)
    monkeypatch.setattr(UserCanvasService, "get_by_id", lambda *a: SimpleNamespace(dsl={}))
    monkeypatch.setattr(Pipeline, "__init__", lambda *a, **k: None)
    monkeypatch.setattr(Pipeline, "__str__", lambda *a: "pipeline")
    monkeypatch.setattr(Pipeline, "run", AsyncMock(return_value={"embedding_token_consumption": 7, "chunks": [{"text": str(i)} for i in range(size)]}))
    monkeypatch.setattr(task_executor.KnowledgebaseService, "get_by_id", lambda *a: SimpleNamespace(embd_id="embedding", name="dataset"))
    monkeypatch.setattr(task_executor, "get_model_config_by_type_and_name", lambda *a: {})
    monkeypatch.setattr(task_executor, "LLMBundle", lambda *a: encoder)
    progress: list[dict[str, Any]] = []
    monkeypatch.setattr(task_executor, "set_progress", lambda *a, **k: progress.append(k))
    monkeypatch.setattr(task_executor, "_persist_pdf_outline_from_storage", AsyncMock())
    monkeypatch.setattr(task_executor, "get_schema", AsyncMock(return_value={}))
    monkeypatch.setattr(task_executor, "record_task_pipeline", lambda *a: None)
    monkeypatch.setattr(db, "get_bind", lambda: None)
    inserted = AsyncMock(return_value=False)
    monkeypatch.setattr(task_executor, "insert_chunks", inserted)
    copies = track_copies(monkeypatch)
    await task_executor.run_dataflow(db, {"id": "task", "dataflow_id": "flow", "doc_id": "doc", "kb_id": "kb", "tenant_id": "tenant", "task_type": "dataflow", "name": "document"})
    assert not any(p.get("prog", 0) < 0 for p in progress), progress
    if size:
        chunks = inserted.call_args.args[4]
        assert [c["content_with_weight"] for c in chunks] == [str(i) for i in range(size)]
        np.testing.assert_array_equal([c["q_3_vec"] for c in chunks], [[i] * 3 for i in range(size)])
        assert chunks[0]["ingest_tokens_int"] == 7 + size * 3
    else:
        inserted.assert_not_called()
        assert encoder.calls == []
    assert copies == ([(size * 3, dtype)] if size > 2 else [])
