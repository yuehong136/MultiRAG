from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from api.db.db_models import Knowledgebase
from core.svr import task_executor


@pytest.mark.parametrize("graph_config", [None, {"use_graphrag": False, "method": "general", "entity_types": ["product"], "resolution": True}, {"use_graphrag": True, "method": "general"}])
@pytest.mark.parametrize("save_ok", [True, False])
async def test_manual_graph_initializes_config_before_generation(db: Session, monkeypatch: pytest.MonkeyPatch, graph_config: dict[str, Any] | None, save_ok: bool) -> None:
    original = {"llm_id": "chat", "raptor": {"use_raptor": True}}
    if graph_config is not None:
        original["graphrag"] = graph_config
    kb = Knowledgebase(id="kb", name="dataset", parser_config=deepcopy(original))
    monkeypatch.setattr(task_executor.KnowledgebaseService, "get_by_id", lambda *args: kb)
    saved: list[dict[str, Any]] = []

    def save(session: Session, kb_id: str, values: dict[str, Any]) -> bool:
        saved.append(values)
        return save_ok

    monkeypatch.setattr(task_executor.KnowledgebaseService, "update_by_id", save)
    monkeypatch.setattr(task_executor, "has_canceled", lambda task_id: False)
    monkeypatch.setattr(task_executor, "get_model_config_by_type_and_name", lambda *args: {})
    model = type("FakeModel", (), {"encode": lambda self, texts: ([[0.0]], 0)})()
    monkeypatch.setattr(task_executor, "LLMBundle", lambda *args, **kwargs: model)
    monkeypatch.setattr(task_executor, "init_kb", AsyncMock())
    graph = AsyncMock(return_value={})
    monkeypatch.setattr(task_executor, "run_graphrag_for_kb", graph)
    progress: list[dict[str, Any]] = []
    monkeypatch.setattr(task_executor, "set_progress", lambda *args, **kwargs: progress.append(kwargs))
    task = {
        "id": "task",
        "task_type": "graphrag",
        "doc_id": "graph_raptor_x",
        "doc_ids": ["doc"],
        "kb_id": "kb",
        "tenant_id": "tenant",
        "from_page": 0,
        "to_page": 1,
        "embd_id": "embedding",
        "llm_id": "chat",
        "language": "Chinese",
        "parser_config": {},
    }
    await task_executor.do_handle_task(db, task)
    if graph_config and graph_config.get("use_graphrag"):
        assert saved == []
        graph.assert_awaited_once()
        assert kb.parser_config == original
        assert progress[-1]["prog"] == 1.0
        return
    config = saved[0]["parser_config"]
    assert config["graphrag"]["use_graphrag"] is True
    assert config["raptor"] == original["raptor"]
    assert kb.parser_config == original  # no mutation of ORM JSON before a successful write
    if graph_config:
        assert config["graphrag"]["method"] == "general"
        assert config["graphrag"]["entity_types"] == ["product"]
        assert config["graphrag"]["resolution"] is True
    else:
        assert config["graphrag"]["method"] == "light"
        assert "person" in config["graphrag"]["entity_types"]
    if save_ok:
        graph.assert_awaited_once()
        assert graph.call_args.kwargs["kb_parser_config"] == config
        assert progress[-1]["prog"] == 1.0
    else:
        graph.assert_not_awaited()
        assert progress[-1]["prog"] == -1.0
        assert "Cannot save" in progress[-1]["msg"]
