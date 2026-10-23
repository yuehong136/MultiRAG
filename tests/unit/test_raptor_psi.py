"""Original-space Psi construction, bounded cost and incomplete-tree safety."""

import asyncio
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from core import raptor as module
from core.raptor import RecursiveAbstractiveProcessing4TreeOrganizedRetrieval as Raptor


def builder(**kwargs: Any) -> Raptor:
    return Raptor(2, SimpleNamespace(max_length=4096), None, "{cluster_content}", tree_builder="psi", **kwargs)


def test_original_space_cosine_and_bounded_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    raptor = builder(psi_exact_max_leaves=3, psi_bucket_size=2)
    inputs = [(str(i), np.array([float(i), float(i % 3 + 1)])) for i in range(24)]
    sizes = []
    original = raptor._rank_leaf_pairs

    def ranked(nodes: list[module._PsiTreeNode]) -> np.ndarray:
        sizes.append(len(nodes))
        return original(nodes)

    monkeypatch.setattr(raptor, "_rank_leaf_pairs", ranked)
    root, leaves = raptor._build_psi_structure(inputs)
    assert len(leaves) == 24
    assert max(sizes) <= 3
    assert all(leaf.parent is not None for leaf in leaves)
    assert all(len(node.children) <= 2 for node in raptor._iter_nodes(root))
    leaves = [module._PsiTreeNode(i, embedding=np.asarray(v)) for i, v in enumerate([[10.0, 0.0], [1.0, 0.1], [0.0, 1.0]])]
    assert set(original(leaves)[0]) == {0, 1}


async def test_psi_materializes_without_umap_and_counts_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    raptor = builder()
    calls = {"chat": 0, "embedding": 0}

    def no_umap(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Psi must rank in the original embedding space")

    async def chat(*args: Any) -> str:
        calls["chat"] += 1
        return f"summary-{calls['chat']}"

    async def encode(text: str) -> np.ndarray:
        calls["embedding"] += 1
        return np.array([1.0, 0.0])

    monkeypatch.setattr(module.umap, "UMAP", no_umap)
    monkeypatch.setattr(raptor, "_chat", chat)
    monkeypatch.setattr(raptor, "_embedding_encode", encode)
    chunks, layers = await raptor([(str(i), np.array([1.0, float(i)])) for i in range(3)], 0)
    assert layers == [(0, 3), (3, 4), (4, 5)]
    assert len(chunks) == 5 and calls == {"chat": 2, "embedding": 2}


async def test_failed_summary_cannot_be_a_complete_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    raptor = builder()

    async def fail(*args: Any) -> str:
        raise RuntimeError("controlled model failure")

    monkeypatch.setattr(raptor, "_chat", fail)
    with pytest.raises(RuntimeError, match="incomplete"):
        await raptor([("a", np.array([1.0, 0.0])), ("b", np.array([0.0, 1.0]))], 0)


async def test_cancel_drains_psi_children(monkeypatch: pytest.MonkeyPatch) -> None:
    raptor = builder()
    started = asyncio.Event()
    drained = asyncio.Event()

    async def blocking(*args: Any) -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()
        return "unreachable"

    monkeypatch.setattr(raptor, "_chat", blocking)
    task = asyncio.create_task(raptor([("a", np.array([1.0, 0.0])), ("b", np.array([0.0, 1.0]))], 0))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drained.is_set()


def test_gmm_single_cluster_and_upper_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    raptor = builder()
    raptor._max_cluster = 1
    assert raptor._get_optimal_clusters(np.ones((3, 2)), 0) == 1
    tested = []

    class GMM:
        def __init__(self, n_components: int, random_state: int) -> None:
            self.n = n_components
            tested.append(n_components)

        def fit(self, values: np.ndarray) -> None:
            pass

        def bic(self, values: np.ndarray) -> float:
            return -float(self.n)

    raptor._max_cluster = 2
    monkeypatch.setattr(module, "GaussianMixture", GMM)
    assert raptor._get_optimal_clusters(np.ones((3, 2)), 0) == 2
    assert tested == [1, 2]


def test_ahc_obeys_max_cluster() -> None:
    raptor = builder()
    labels = raptor._get_clusters_ahc(np.asarray([[0, 0], [0.1, 0.1], [10, 10], [10.1, 10.1]]))
    assert len(set(labels)) == 2
    raptor._max_cluster = 1
    assert set(raptor._get_clusters_ahc(np.asarray([[0, 0], [1, 1], [10, 10]]))) == {0}
