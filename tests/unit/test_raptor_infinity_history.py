"""RAPTOR consumes native Infinity history without changing snapshot payloads."""

import copy
from typing import Any

import numpy as np
import pytest

from core import raptor as raptor_module
from core.svr import task_executor


@pytest.mark.parametrize(("native_infinity", "dimension"), [(True, 3), (True, 768), (False, 768)])
async def test_generation_uses_backend_history_columns_and_preserves_extra(monkeypatch: pytest.MonkeyPatch, native_infinity: bool, dimension: int) -> None:
    vector_field = f"q_{dimension}_vec" if native_infinity else "vector"
    content_field = "content" if native_infinity else "content_with_weight"
    rows = [{"id": str(i), "doc_id": "doc", content_field: f"source {i}", vector_field: [0.1] * dimension} for i in range(2)]
    original = copy.deepcopy(rows)

    async def history(doc_id: str, *args: Any) -> list[dict[str, Any]]:
        return rows if doc_id == "doc" else []

    class ControlledRaptor:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __call__(self, inputs: list[tuple[str, np.ndarray]], *args: Any) -> tuple[list[tuple[str, np.ndarray]], list[tuple[int, int]]]:
            assert [text for text, _ in inputs] == ["source 0", "source 1"]
            assert all(vector.shape == (dimension,) for _, vector in inputs)
            return [*inputs, ("summary", np.full(dimension, 0.2))], [(0, 2), (2, 3)]

    monkeypatch.setattr(task_executor, "_raptor_history", history)
    monkeypatch.setitem(vars(task_executor.settings), "DOC_ENGINE_INFINITY", native_infinity)
    monkeypatch.setattr(raptor_module, "RecursiveAbstractiveProcessing4TreeOrganizedRetrieval", ControlledRaptor)
    config = {"raptor": {"scope": "file", "prompt": "{cluster_content}", "max_token": 32, "threshold": 0.1, "ext": {"tree_builder": "psi"}}}
    chunks, tokens, previous = await task_executor.run_raptor_for_kb({"id": "task", "tenant_id": "tenant", "kb_id": "kb", "name": "source.txt"}, config, None, None, dimension, doc_ids=["doc"])
    assert len(chunks) == 1 and tokens > 0 and previous == {"doc": {}}
    assert chunks[0][vector_field] == [0.2] * dimension
    assert chunks[0]["extra"] == {"raptor_method": "psi", "raptor_clustering_method": "gmm"}
    prepared = task_executor._prepare_raptor_chunks(chunks, {"fields": []})
    assert prepared[0]["extra"] == chunks[0]["extra"]
    assert isinstance(prepared[0]["extra"], dict) and prepared[0]["extra"] is not chunks[0]["extra"]
    assert rows == original
