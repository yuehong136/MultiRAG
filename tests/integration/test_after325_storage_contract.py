"""Real SQL cleanup contracts with explicitly controlled object/index boundaries."""

import copy
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, DocumentMetadata, File, File2Document, Knowledgebase, Task
from common import resources
from common.constants import FileSource
from core.nlp import search
from tests.support.resource_journal import ResourceJournal


class ControlledStore:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.collections: set[str] = set()

    def db_type(self) -> str:
        return "milvus"

    def has_collection(self, index_name: str) -> bool:
        return index_name in self.collections

    def search(self, fields: list[str], highlights: list[str], condition: dict[str, Any], expressions: list[Any], order: Any, offset: int, limit: int, index: str, datasets: list[str]) -> list[Any]:
        # These cases have no image or graph references. Chunk deletion below
        # retains real document/dataset filtering, independently read back.
        return []

    def get_doc_ids(self, result: list[Any]) -> list[str]:
        return []

    def get_fields(self, result: list[Any], fields: list[str]) -> dict[str, Any]:
        return {}

    def delete(self, condition: dict[str, Any], index_name: str, dataset_id: str) -> int:
        before = self.rows.get(dataset_id, [])
        kept = [row for row in before if not all(row.get(key) == value for key, value in condition.items())]
        self.rows[dataset_id] = kept
        return len(before) - len(kept)

    def delete_idx(self, index_name: str, dataset_id: str) -> None:
        self.rows.pop(dataset_id, None)
        self.collections.discard(index_name)


class ControlledStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def obj_exist(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.objects

    def rm(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)

    def remove_bucket(self, bucket: str) -> None:
        self.objects = {key: value for key, value in self.objects.items() if key[0] != bucket}


@pytest.fixture
def cleanup_rows(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    ids = {key: uuid4().hex for key in ["owner", "other", "kb", "foreign", "doc", "foreign_doc", "folder", "foreign_folder", "task"]}
    journal = ResourceJournal("after325-dataset-missing-file-link")
    journal.event("reserve-sql-and-controlled-storage", database=bootstrapped_engine.url.database, ids=ids, boundaries=["object bytes", "chunk index", "task cancellation flag"])
    store, storage = ControlledStore(), ControlledStorage()
    monkeypatch.setitem(resources._state, "secret_key", "after325-controlled-cleanup-session")
    monkeypatch.setitem(resources._state, "doc_store", store)
    monkeypatch.setitem(resources._state, "storage", storage)
    from api.db.services import task_service

    cancellations: list[tuple[str, tuple[str, ...] | None]] = []

    def cancel(db: Session, doc_id: str, *, task_ids: tuple[str, ...] | None = None) -> None:
        cancellations.append((doc_id, task_ids))

    monkeypatch.setattr(task_service, "cancel_all_task_of", cancel)
    for key, owner_key, doc_key, folder_key in [("kb", "owner", "doc", "folder"), ("foreign", "other", "foreign_doc", "foreign_folder")]:
        store.collections.add(search.index_name_one(ids[owner_key], "same-name"))
        store.rows[ids[key]] = [{"doc_id": ids[doc_key], "kb_id": ids[key], "content_with_weight": key}]
        storage.objects[ids[key], "source.txt"] = key.encode()
        with Session(bootstrapped_engine) as db:
            db.add(Knowledgebase(id=ids[key], tenant_id=ids[owner_key], created_by=ids[owner_key], name="same-name", embd_id="controlled-unused", doc_num=1, chunk_num=1, token_num=7))
            db.add(Document(id=ids[doc_key], kb_id=ids[key], created_by=ids[owner_key], name="source.txt", location="source.txt", type="doc", parser_id="naive", chunk_num=1, token_num=7))
            db.add(File(id=ids[folder_key], tenant_id=ids[owner_key], created_by=ids[owner_key], parent_id="", name="same-name", type="folder", source_type=FileSource.KNOWLEDGEBASE))
            db.add(DocumentMetadata(id=ids[doc_key], tenant_id=ids[owner_key], kb_id=ids[key], meta_fields={"source": [key]}))
            db.commit()
    with Session(bootstrapped_engine) as db:
        db.add(Task(id=ids["task"], doc_id=ids["doc"], from_page=0, to_page=1))
        db.commit()
    env = {"ids": ids, "engine": bootstrapped_engine, "store": store, "storage": storage, "journal": journal, "cancellations": cancellations}
    try:
        yield env
    finally:
        scoped = [
            (Task, Task.id == ids["task"]),
            (File2Document, File2Document.document_id.in_([ids["doc"], ids["foreign_doc"]])),
            (DocumentMetadata, DocumentMetadata.id.in_([ids["doc"], ids["foreign_doc"]])),
            (Document, Document.id.in_([ids["doc"], ids["foreign_doc"]])),
            (File, File.id.in_([ids["folder"], ids["foreign_folder"]])),
            (Knowledgebase, Knowledgebase.id.in_([ids["kb"], ids["foreign"]])),
        ]
        with bootstrapped_engine.begin() as connection:
            for model, condition in scoped:
                connection.execute(sa.delete(model).where(condition))
        with bootstrapped_engine.connect() as reader:
            remaining = {model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(condition)) for model, condition in scoped}
        assert not any(remaining.values()), remaining
        journal.event("sql-independent-cleanup-readback", remaining=remaining)


def _cleanup_snapshot(env: dict[str, Any]) -> dict[str, Any]:
    with env["engine"].connect() as reader:
        sql = {
            model.__tablename__: [dict(row) for row in reader.execute(sa.select(model.__table__).order_by(model.id)).mappings()]
            for model in [Knowledgebase, Document, File, File2Document, Task, DocumentMetadata]
        }
    return {"sql": sql, "index": copy.deepcopy(env["store"].rows), "objects": copy.deepcopy(env["storage"].objects)}


def test_dataset_delete_without_file_link_commits_and_keeps_other_tenant(cleanup_rows: dict[str, Any]) -> None:
    from api.apps.services.dataset_api_service import delete_datasets

    env, ids = cleanup_rows, cleanup_rows["ids"]
    before = _cleanup_snapshot(env)
    with Session(env["engine"]) as db:
        assert delete_datasets(db, ids["owner"], [ids["kb"]]) == (True, None)
    after = _cleanup_snapshot(env)
    with env["engine"].connect() as reader:
        for model, identifier in [(Knowledgebase, ids["kb"]), (Document, ids["doc"]), (File, ids["folder"]), (Task, ids["task"]), (DocumentMetadata, ids["doc"])]:
            assert reader.scalar(sa.select(sa.func.count()).select_from(model).where(model.id == identifier)) == 0
        assert reader.scalar(sa.select(sa.func.count()).select_from(File2Document).where(File2Document.document_id == ids["doc"])) == 0
    assert env["cancellations"] == [(ids["doc"], (ids["task"],))]
    for model in [Knowledgebase, Document, File, DocumentMetadata]:
        previous = [row for row in before["sql"][model.__tablename__] if row["id"] in [ids["foreign"], ids["foreign_doc"], ids["foreign_folder"]]]
        assert [row for row in after["sql"][model.__tablename__] if row["id"] in [ids["foreign"], ids["foreign_doc"], ids["foreign_folder"]]] == previous
    assert after["index"] == {ids["foreign"]: before["index"][ids["foreign"]]}
    assert after["objects"] == {(ids["foreign"], "source.txt"): b"foreign"}
    env["journal"].event("dataset-delete-independent-readback", deleted_ids=[ids["kb"], ids["doc"], ids["folder"], ids["task"]], foreign_unchanged=True, dangling_link_or_object=False)


def test_dataset_delete_preflights_foreign_selection_before_any_side_effect(cleanup_rows: dict[str, Any]) -> None:
    from api.apps.services.dataset_api_service import delete_datasets

    env, ids = cleanup_rows, cleanup_rows["ids"]
    before = _cleanup_snapshot(env)
    with Session(env["engine"]) as db:
        ok, error = delete_datasets(db, ids["owner"], [ids["kb"], ids["foreign"]])
    assert not ok and "lacks permission" in error
    assert _cleanup_snapshot(env) == before
    assert env["cancellations"] == []
    env["journal"].event("denied-delete-independent-readback", unchanged=True)
