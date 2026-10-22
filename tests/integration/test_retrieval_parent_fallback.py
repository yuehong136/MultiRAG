"""Real SQL document/KB scope with a controlled parent-index transport."""

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase
from core.nlp import search


@pytest.fixture
def parent_scope(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    ids = [uuid4().hex for _ in range(6)]
    kbs, docs = ids[:3], ids[3:]
    tenants = ["parent-tenant-a", "parent-tenant-b", "outside"]
    with Session(bootstrapped_engine) as db:
        for kb, doc, tenant in zip(kbs, docs, tenants, strict=True):
            db.add(Knowledgebase(id=kb, tenant_id=tenant, created_by=tenant, name=kb, embd_id="controlled", status="1"))
            db.add(Document(id=doc, kb_id=kb, created_by=tenant, type="txt", parser_id="naive", status="1"))
        db.commit()

    @contextmanager
    def connection() -> Iterator[Session]:
        with Session(bootstrapped_engine) as db:
            yield db

    monkeypatch.setattr(search, "db_connection", connection)
    dealer = search.Dealer.__new__(search.Dealer)
    dealer.dataStore = SimpleNamespace(get=Mock(return_value=None))
    children = [
        {"chunk_id": "child-" + kb, "kb_id": kb, "doc_id": doc, "mom_id": "shared-parent", "content_ltks": "matching child", "text": "child text", "similarity": score, "vector": [0.2, 0.3]}
        for kb, doc, score in zip(kbs, docs, [0.8, 0.9, 1.0], strict=True)
    ]
    try:
        yield {"engine": bootstrapped_engine, "dealer": dealer, "kbs": kbs, "docs": docs, "tenants": tenants[:2], "children": children}
    finally:
        with Session(bootstrapped_engine) as db:
            db.execute(sa.delete(Document).where(Document.id.in_(docs)))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id.in_(kbs)))
            db.commit()


def test_missing_parent_retains_children_with_exact_index_and_no_mutation(parent_scope: dict[str, Any]) -> None:
    env = parent_scope
    children = [env["children"][0], env["children"][1], env["children"][0], env["children"][2]]
    original = deepcopy(children)
    result = env["dealer"].retrieval_by_children(children, env["tenants"])
    assert result == [children[1], children[0]]
    assert children == original
    assert env["dealer"].dataStore.get.call_args_list[0].args == ("shared-parent", search.index_name_one(env["tenants"][0], env["kbs"][0]), [env["kbs"][0]])
    assert env["dealer"].dataStore.get.call_args_list[1].args == ("shared-parent", search.index_name_one(env["tenants"][1], env["kbs"][1]), [env["kbs"][1]])


def test_same_parent_id_in_two_datasets_never_coalesces(parent_scope: dict[str, Any]) -> None:
    env = parent_scope

    def fetch(identifier: str, index: str, kb_ids: list[str]) -> dict[str, Any]:
        kb = kb_ids[0]
        position = env["kbs"].index(kb)
        assert index == search.index_name_one(env["tenants"][position], kb)
        return {"kb_id": kb, "doc_id": env["docs"][position], "content_with_weight": "parent for " + kb, "available_int": 0}

    env["dealer"].dataStore.get.side_effect = fetch
    result = env["dealer"].retrieval_by_children(env["children"][:2], env["tenants"])
    assert [row["kb_id"] for row in result] == list(reversed(env["kbs"][:2]))
    assert all(row["text"] == "parent for " + row["kb_id"] and row["vector"] == [0.2, 0.3] for row in result)


@pytest.mark.parametrize("state", ["deleted-doc", "disabled-doc", "disabled-kb", "deleted-kb"])
def test_sql_delete_and_disable_never_resurrect_children(parent_scope: dict[str, Any], state: str) -> None:
    env = parent_scope
    model = Document if state.endswith("doc") else Knowledgebase
    identifier = env["docs"][0] if model is Document else env["kbs"][0]
    with Session(env["engine"]) as writer:
        if state.startswith("deleted"):
            writer.execute(sa.delete(model).where(model.id == identifier))
        else:
            writer.execute(sa.update(model).where(model.id == identifier).values(status="0"))
        writer.commit()
    with Session(env["engine"]) as reader:
        row = reader.get(model, identifier)
        assert row is None if state.startswith("deleted") else row.status == "0"
    for _ in range(2):
        result = env["dealer"].retrieval_by_children(env["children"][:2], env["tenants"])
        assert result == [env["children"][1]]
    assert all(call.args[2] == [env["kbs"][1]] for call in env["dealer"].dataStore.get.call_args_list)


@pytest.mark.parametrize("parent", [{"kb_id": "wrong"}, {"doc_id": "wrong"}])
def test_invalid_parent_cannot_replace_child(parent_scope: dict[str, Any], parent: dict[str, Any]) -> None:
    env = parent_scope
    value = {"kb_id": env["kbs"][0], "doc_id": env["docs"][0], "content_with_weight": "disabled", **parent}
    env["dealer"].dataStore.get.return_value = value
    assert env["dealer"].retrieval_by_children(env["children"][:1], env["tenants"]) == env["children"][:1]


def test_existing_parent_candidate_is_not_duplicated(parent_scope: dict[str, Any]) -> None:
    env = parent_scope
    child = env["children"][0]
    parent = {**child, "chunk_id": "shared-parent", "mom_id": "", "similarity": 0.95}
    env["dealer"].dataStore.get.return_value = {"kb_id": child["kb_id"], "doc_id": child["doc_id"], "content_with_weight": "parent", "available_int": 0}
    result = env["dealer"].retrieval_by_children([child, parent], env["tenants"])
    assert result == [parent]


def test_store_failure_propagates_and_keeps_input_and_sql(parent_scope: dict[str, Any]) -> None:
    env = parent_scope
    original = deepcopy(env["children"])
    env["dealer"].dataStore.get.side_effect = ConnectionError("index unavailable")
    with pytest.raises(ConnectionError, match="index unavailable"):
        env["dealer"].retrieval_by_children(env["children"], env["tenants"])
    assert env["children"] == original
    with Session(env["engine"]) as reader:
        assert set(reader.scalars(sa.select(Document.id).where(Document.id.in_(env["docs"])))) == set(env["docs"])
        assert set(reader.scalars(sa.select(Knowledgebase.id).where(Knowledgebase.id.in_(env["kbs"])))) == set(env["kbs"])
