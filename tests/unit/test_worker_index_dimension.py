"""Index creation consumes the successful task probe without another provider."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from beartype.roar import BeartypeCallHintParamViolation
from sqlalchemy.orm import Session

from core.svr import task_executor


@pytest.mark.parametrize("backend", ["milvus", "elasticsearch"])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("explicit_model", [False, True])
async def test_task_reuses_single_probe_for_new_and_existing_indexes(db: Session, monkeypatch: pytest.MonkeyPatch, backend: str, existing: bool, explicit_model: bool) -> None:
    task = {
        "id": "task",
        "task_type": "dataflow",
        "doc_id": "doc",
        "kb_id": "kb",
        "tenant_id": "tenant",
        "from_page": 0,
        "to_page": 1,
        "embd_id": "model" if explicit_model else "",
        "llm_id": "chat",
        "language": "Chinese",
        "name": "file",
        "parser_id": "naive",
    }
    encode = Mock(return_value=([[0.0] * 5], 1))
    bundle = Mock(return_value=SimpleNamespace(encode=encode))
    store = SimpleNamespace(db_type=lambda: backend, has_collection=Mock(return_value=existing), index_exist=Mock(return_value=existing), ensure_collection_loaded=Mock(), create_idx=Mock())
    monkeypatch.setitem(vars(task_executor.settings), "docStoreConn", store)
    monkeypatch.setattr(task_executor.search, "index_name_one", lambda tenant, name: "owned-index")
    monkeypatch.setattr(task_executor, "has_canceled", lambda task_id: False)
    monkeypatch.setattr(task_executor, "LLMBundle", bundle)
    monkeypatch.setattr(task_executor, "get_model_config_by_type_and_name", lambda *args: {"explicit": True})
    monkeypatch.setattr(task_executor, "get_tenant_default_model_by_type", lambda *args: {"explicit": False})
    monkeypatch.setattr(task_executor.KnowledgebaseService, "get_by_id", lambda *args: SimpleNamespace(name="dataset"))
    create_milvus = AsyncMock()
    flow = AsyncMock()
    monkeypatch.setattr(task_executor, "_create_milvus_collection", create_milvus)
    monkeypatch.setattr(task_executor, "run_dataflow", flow)
    await task_executor.do_handle_task(db, task)
    bundle.assert_called_once()
    assert bundle.call_args.args[2] == {"explicit": explicit_model}
    encode.assert_called_once_with(["ok"])
    flow.assert_awaited_once()
    if backend == "milvus":
        assert create_milvus.await_args_list == ([] if existing else [(("owned-index", 5), {})])
        assert store.ensure_collection_loaded.call_count == int(existing)
        store.create_idx.assert_not_called()
    else:
        assert store.create_idx.call_args_list == ([] if existing else [(("owned-index", "kb", 5, "naive"), {})])
        create_milvus.assert_not_called()


@pytest.mark.parametrize("failure", ["probe", "create"])
async def test_probe_or_creation_failure_never_reaches_dataflow_or_success(db: Session, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    task = {
        "id": "task",
        "task_type": "dataflow",
        "doc_id": "doc",
        "kb_id": "kb",
        "tenant_id": "tenant",
        "from_page": 0,
        "to_page": 1,
        "embd_id": "model",
        "llm_id": "chat",
        "name": "file",
        "parser_id": "naive",
    }
    encode = Mock(side_effect=ValueError("probe failed")) if failure == "probe" else Mock(return_value=([[0.0] * 5], 1))
    create = Mock(side_effect=ValueError("create failed"))
    store = SimpleNamespace(db_type=lambda: "elasticsearch", index_exist=Mock(return_value=False), create_idx=create)
    monkeypatch.setitem(vars(task_executor.settings), "docStoreConn", store)
    monkeypatch.setattr(task_executor, "has_canceled", lambda task_id: False)
    monkeypatch.setattr(task_executor, "LLMBundle", lambda *args, **kwargs: SimpleNamespace(encode=encode))
    monkeypatch.setattr(task_executor, "get_model_config_by_type_and_name", lambda *args: {})
    monkeypatch.setattr(task_executor.KnowledgebaseService, "get_by_id", lambda *args: SimpleNamespace(name="dataset"))
    progress = Mock()
    flow = AsyncMock()
    monkeypatch.setattr(task_executor, "set_progress", progress)
    monkeypatch.setattr(task_executor, "run_dataflow", flow)
    with pytest.raises(ValueError, match=f"{failure} failed"):
        await task_executor.do_handle_task(db, task)
    assert create.call_count == int(failure == "create")
    flow.assert_not_called()
    assert not any(call.kwargs.get("prog") == 1 for call in progress.call_args_list)


@pytest.mark.parametrize("dimension", [0, -1, True, None, 1.5, "5"])
async def test_invalid_dimension_has_no_storage_effect(monkeypatch: pytest.MonkeyPatch, dimension: Any) -> None:
    store = Mock()
    monkeypatch.setitem(vars(task_executor.settings), "docStoreConn", store)
    error = ValueError if isinstance(dimension, int) else BeartypeCallHintParamViolation
    with pytest.raises(error, match="positive integer" if isinstance(dimension, int) else "vector_size"):
        await task_executor.init_kb({"tenant_id": "tenant"}, "dataset", dimension)
    assert store.mock_calls == []
