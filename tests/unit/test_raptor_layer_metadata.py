from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from core.raptor import RecursiveAbstractiveProcessing4TreeOrganizedRetrieval as Raptor
from core.svr import task_executor


async def test_raptor_returns_layer_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    raptor = Raptor(4, SimpleNamespace(max_length=1024), None, "{cluster_content}")

    async def fake_chat(system: str, history: list[dict[str, str]], gen_conf: dict[str, int]) -> str:
        return "combined summary"

    async def fake_embedding_encode(text: str) -> np.ndarray:
        return np.array([0.5, 0.5])

    monkeypatch.setattr(raptor, "_chat", fake_chat)
    monkeypatch.setattr(raptor, "_embedding_encode", fake_embedding_encode)

    assert await raptor([], 42) == ([], [])
    assert await raptor([("only", np.array([1.0, 0.0]))], 42) == ([], [])

    chunks, layers = await raptor([("first", np.array([1.0, 0.0])), ("second", np.array([0.0, 1.0]))], 42)

    assert [text for text, _ in chunks] == ["first", "second", "combined summary"]
    assert layers == [(0, 2), (2, 3)]


async def test_indexed_raptor_summaries_keep_their_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    original = [(f"original {i}", np.array([1.0, 0.0, 0.0])) for i in range(3)]
    summaries = [(f"summary {i}", np.array([0.0, 1.0, 0.0])) for i in range(3)]

    class FakeRaptor:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __call__(
            self,
            chunks: list[tuple[str, np.ndarray]],
            random_state: int,
            callback: Callable[..., Any] | None = None,
            task_id: str = "",
        ) -> tuple[list[tuple[str, np.ndarray]], list[tuple[int, int]]]:
            assert [text for text, _ in chunks] == [text for text, _ in original]
            for (_, vector), (_, expected) in zip(chunks, original, strict=True):
                np.testing.assert_array_equal(vector, expected)
            return chunks + summaries, [(0, 3), (3, 5), (5, 6)]

    async def no_existing_raptor_chunks(doc_id: str, tenant_id: str, kb_id: str) -> bool:
        return False

    def fake_chunk_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"content_with_weight": text, "q_3_vec": vector.tolist()} for text, vector in original]

    def callback(**kwargs: Any) -> None:
        pass

    monkeypatch.setattr(task_executor, "Raptor", FakeRaptor)
    monkeypatch.setattr(task_executor, "has_raptor_chunks", no_existing_raptor_chunks)
    monkeypatch.setattr(
        task_executor.settings,
        "retriever",
        SimpleNamespace(chunk_list=fake_chunk_list),
        raising=False,
    )

    row = {"tenant_id": "tenant", "kb_id": "kb", "id": "task", "name": "knowledge base", "pagerank": 0}
    config = {"raptor": {"scope": "file", "prompt": "{cluster_content}", "max_token": 512, "threshold": 0.1, "random_seed": 42}}
    indexed, token_count = await task_executor.run_raptor_for_kb(row, config, None, None, 3, callback=callback, doc_ids=["doc"])

    assert [chunk["content_with_weight"] for chunk in indexed] == ["summary 0", "summary 1", "summary 2"]
    assert [chunk["raptor_layer_int"] for chunk in indexed] == [1, 1, 2]
    assert all(chunk["raptor_kwd"] == "raptor" and chunk["doc_id"] == "doc" for chunk in indexed)
    assert token_count > 0
