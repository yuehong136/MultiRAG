"""Real Canvas and SQL authorization reach a controlled retriever with top_k."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.tools import retrieval as module
from agent.tools.retrieval import Retrieval
from api.db.db_models import Knowledgebase
from tests.support.dataset_access_permissions import permission_rows as permission_rows


@pytest.mark.parametrize(("top_k", "keyword_weight"), [(1, 0), (37, 0.23), (2048, 1)])
async def test_workflow_retrieval_preserves_limit_weight_and_sql_scope(
    bootstrapped_engine: sa.Engine,
    permission_rows: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
    top_k: int,
    keyword_weight: float,
) -> None:
    ids = permission_rows
    record_property("scratch_database", bootstrapped_engine.url.database)
    record_property("dataset_id", ids["private"])
    record_property("top_k", top_k)
    record_property("keywords_similarity_weight", keyword_weight)
    active_sessions = 0

    @contextmanager
    def connection() -> Iterator[Session]:
        nonlocal active_sessions
        with Session(bootstrapped_engine) as db:
            active_sessions += 1
            try:
                yield db
            finally:
                active_sessions -= 1

    monkeypatch.setattr(module, "db_connection", connection)
    bundle = object()

    async def embedding(*args: Any, **kwargs: Any) -> object:
        assert active_sessions == 0
        return bundle

    monkeypatch.setattr(module, "build_named_bundle_async", embedding)
    monkeypatch.setattr(Retrieval, "_rank_feature", staticmethod(lambda *_: {}))
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def recall(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert active_sessions == 0
        calls.append((args, kwargs))
        return {"chunks": [], "doc_aggs": []}

    monkeypatch.setitem(vars(module.settings), "retriever", SimpleNamespace(retrieval=recall, retrieval_by_children=lambda chunks, _: chunks))
    dsl = {
        "components": {
            "begin": {"obj": {"component_name": "Begin", "params": {}}, "upstream": [], "downstream": ["Retrieval:limit"]},
            "Retrieval:limit": {
                "obj": {
                    "component_name": "Retrieval",
                    "params": {
                        "query": "{sys.query}",
                        "dataset_ids": [ids["private"]],
                        "top_n": 3,
                        "top_k": top_k,
                        "keywords_similarity_weight": keyword_weight,
                        "empty_response": "No controlled results",
                    },
                },
                "upstream": ["begin"],
                "downstream": [],
            },
        },
        "retrieval": [],
        "path": [],
    }
    canvas = Canvas(json.dumps(dsl), tenant_id=ids["owner"])
    monkeypatch.setattr(canvas, "is_canceled", lambda: False)
    with Session(bootstrapped_engine) as db:
        before = list(db.execute(sa.select(Knowledgebase.__table__).where(Knowledgebase.tenant_id == ids["tenant"])))
    try:
        events = [event async for event in canvas.run(query="user: exact recall")]
        assert not canvas.error
        finished = [event["data"] for event in events if event["event"] == "node_finished" and event["data"]["component_id"] == "Retrieval:limit"]
        assert len(finished) == 1
        assert not finished[0]["error"]
        assert finished[0]["outputs"]["formalized_content"] == "No controlled results"
        assert len(calls) == 1
        args, options = calls[0]
        assert args[0] == "exact recall"
        assert args[2] is bundle
        assert args[3:5] == ([ids["tenant"]], ["Shared permission name"])
        assert args[6:9] == (3, 0.2, 1 - keyword_weight)
        assert options["top"] == top_k
        assert options["kb_ids"] == [ids["private"]]
        assert options["doc_ids"] == []
        with Session(bootstrapped_engine) as db:
            assert list(db.execute(sa.select(Knowledgebase.__table__).where(Knowledgebase.tenant_id == ids["tenant"]))) == before
    finally:
        canvas._thread_pool.shutdown(wait=True)
