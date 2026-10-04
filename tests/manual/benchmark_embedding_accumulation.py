"""Run with `uv run python -m tests.manual.benchmark_embedding_accumulation`.

Compare the previous accumulation loop with the actual BuiltinEmbed.encode.
Model inference is controlled; timings measure local accumulation, not a service.
"""

import json
from time import perf_counter
from typing import Any
from unittest.mock import patch

import numpy as np

from core.llm.embedding import BuiltinEmbed


class PreparedEncoder:
    def __init__(self, vectors: np.ndarray) -> None:
        self.vectors = vectors
        self.offset = 0

    def encode(self, texts: list[str]) -> tuple[np.ndarray, int]:
        start = self.offset
        self.offset += len(texts)
        return self.vectors[start : self.offset], len(texts)


def legacy_encode(encoder: PreparedEncoder, texts: list[str]) -> tuple[np.ndarray | None, int]:
    result = None
    tokens = 0
    for i in range(0, len(texts), 16):
        vectors, count = encoder.encode(texts[i : i + 16])
        tokens += count
        result = vectors if result is None else np.concatenate((result, vectors), axis=0)
    return result, tokens


def measure(vectors: np.ndarray, optimized: bool) -> dict[str, Any]:
    texts = ["text"] * len(vectors)
    concatenate = np.concatenate
    copies: list[int] = []

    def measured_concat(arrays: Any, *args: Any, **kwargs: Any) -> np.ndarray:
        result = concatenate(arrays, *args, **kwargs)
        copies.append(result.nbytes)
        return result

    timings = []
    for _ in range(5):
        encoder = PreparedEncoder(vectors)
        model = BuiltinEmbed.__new__(BuiltinEmbed)
        model._model = encoder
        copies.clear()
        with patch.object(np, "concatenate", measured_concat):
            started = perf_counter()
            result, tokens = model.encode(texts) if optimized else legacy_encode(encoder, texts)
            timings.append(perf_counter() - started)
        np.testing.assert_array_equal(result, vectors)
        assert result.dtype == vectors.dtype and result.shape == vectors.shape
        assert tokens == len(texts)
    return {"concat_calls": len(copies), "copied_bytes": sum(copies), "best_seconds": min(timings)}


def main() -> None:
    for batches in [8, 32, 128]:
        vectors = np.arange(batches * 16 * 768, dtype=np.float32).reshape(-1, 768)
        old = measure(vectors, False)
        new = measure(vectors, True)
        assert old["concat_calls"] == batches - 1
        assert old["copied_bytes"] == (batches * (batches + 1) // 2 - 1) * 16 * 768 * 4
        assert new["concat_calls"] == 1 and new["copied_bytes"] == vectors.nbytes
        print(json.dumps({"batches": batches, "old": old, "new": new, "copy_ratio": old["copied_bytes"] / new["copied_bytes"]}, sort_keys=True))


if __name__ == "__main__":
    main()
