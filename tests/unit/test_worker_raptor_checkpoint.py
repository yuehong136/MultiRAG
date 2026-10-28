"""Existing trees report a checkpoint without claiming terminal completion."""

import copy
from typing import Any
from unittest.mock import Mock

import pytest

from core import raptor as raptor_module
from core.svr import task_executor


@pytest.mark.parametrize("scope", ["file", "dataset"])
async def test_matching_tree_checkpoint_is_message_only_and_preserves_history(monkeypatch: pytest.MonkeyPatch, scope: str) -> None:
    target = "doc" if scope == "file" else task_executor.GRAPH_RAPTOR_FAKE_DOC_ID
    rows = [
        {"id": "summary", "doc_id": target, "raptor_kwd": "raptor", "content_with_weight": "summary", "q_5_vec": [0.0] * 5, "extra": {"raptor_method": "raptor", "raptor_clustering_method": "gmm"}}
    ]
    before = copy.deepcopy(rows)

    async def history(doc: str, *args: Any) -> list[dict[str, Any]]:
        return rows if doc == target else []

    model = Mock(side_effect=AssertionError("checkpoint called provider"))
    monkeypatch.setattr(task_executor, "_raptor_history", history)
    monkeypatch.setattr(raptor_module, "RecursiveAbstractiveProcessing4TreeOrganizedRetrieval", model)
    monkeypatch.setitem(vars(task_executor.settings), "DOC_ENGINE_INFINITY", False)
    progress: list[dict[str, Any]] = []
    chunks, tokens, previous = await task_executor.run_raptor_for_kb(
        {"id": "task", "tenant_id": "tenant", "kb_id": "kb", "name": "dataset"},
        {"raptor": {"scope": scope}},
        None,
        None,
        5,
        callback=lambda **kwargs: progress.append(kwargs),
        doc_ids=["doc"],
    )
    assert chunks == [] and tokens == 0 and previous == {}
    assert len(progress) == 1 and "checkpoint" in progress[0]["msg"]
    assert "prog" not in progress[0]
    model.assert_not_called()
    assert rows == before
