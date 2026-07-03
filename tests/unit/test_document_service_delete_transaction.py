import json
from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session

from api.apps import dataset_app
from api.db.db_models import Document
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import (
    DeleteDocumentSnapshot,
    DocumentService,
    OrphanFilePayload,
)
from api.utils.api_utils import RetCode


class _FakeMappingResult:
    def __init__(self, one: dict | None = None, rows: list[dict] | None = None) -> None:
        self._one = one
        self._rows = rows or []

    def mappings(self) -> "_FakeMappingResult":
        return self

    def one_or_none(self) -> dict | None:
        return self._one

    def all(self) -> list[dict]:
        return list(self._rows)


class _FakeMutationResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _PgConflictError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _build_conflict() -> sa_exc.OperationalError:
    return sa_exc.OperationalError("SELECT 1", {}, _PgConflictError("40P01"))


class _SessionDouble(Session):
    def __init__(self, side_effects: list[object]) -> None:
        super().__init__()
        self._side_effects = list(side_effects)
        self.executed_statements: list[object] = []
        self.rollback_mock = MagicMock()
        self._in_transaction = False

    def execute(self, statement, *args, **kwargs):
        self.executed_statements.append(statement)
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect

    def begin(self):
        return nullcontext()

    def rollback(self) -> None:
        self.rollback_mock()

    def in_transaction(self) -> bool:
        return self._in_transaction

    @property
    def dirty(self) -> set[object]:
        return set()

    @property
    def new(self) -> set[object]:
        return set()

    @property
    def deleted(self) -> set[object]:
        return set()


def _build_session(*side_effects: object) -> _SessionDouble:
    db = _SessionDouble(list(side_effects))
    return db


def _successful_delete_side_effects(*, remaining_refs: int = 0, delete_file_rowcount: int = 1) -> list[object]:
    remaining_ref_rows = []
    if remaining_refs:
        remaining_ref_rows.append({"file_id": "file-1", "ref_count": remaining_refs})

    side_effects: list[object] = [
        _FakeMappingResult(
            one={
                "id": "doc-1",
                "kb_id": "kb-1",
                "token_num": 11,
                "chunk_num": 7,
                "thumbnail": "thumb.png",
                "location": "doc-1.pdf",
            }
        ),
        _FakeMappingResult(
            one={
                "id": "kb-1",
                "name": "Dataset A",
                "tenant_id": "tenant-1",
            }
        ),
        _FakeMappingResult(rows=[{"id": "link-1", "file_id": "file-1"}]),
        _FakeMappingResult(
            rows=[
                {
                    "id": "file-1",
                    "parent_id": "bucket-1",
                    "location": "doc-1.pdf",
                    "source_type": "knowledgebase",
                }
            ]
        ),
        _FakeMappingResult(rows=remaining_ref_rows),
        _FakeMutationResult(1),  # delete tasks
        _FakeMutationResult(1),  # delete file2document
    ]
    if remaining_refs == 0:
        side_effects.append(_FakeMutationResult(delete_file_rowcount))
    side_effects.extend(
        [
            _FakeMutationResult(1),  # delete document
            _FakeMutationResult(1),  # update kb counters
        ]
    )
    return side_effects


def test_delete_document_db_state_returns_none_when_document_missing() -> None:
    db = _build_session(_FakeMappingResult(one=None))

    assert DocumentService._delete_document_db_state(db, "missing-doc") is None
    assert len(db.executed_statements) == 1


def test_delete_document_db_state_deletes_orphan_file_and_locks_rows() -> None:
    db = _build_session(*_successful_delete_side_effects())

    snapshot = DocumentService._delete_document_db_state(db, "doc-1")

    assert snapshot == DeleteDocumentSnapshot(
        doc_id="doc-1",
        kb_id="kb-1",
        kb_name="Dataset A",
        tenant_id="tenant-1",
        thumbnail="thumb.png",
        location="doc-1.pdf",
        candidate_file_ids=("file-1",),
        orphan_file_payloads=(
            OrphanFilePayload(
                file_id="file-1",
                bucket="kb-1",
                location="doc-1.pdf",
            ),
        ),
    )

    executed_statements = db.executed_statements[:4]
    assert all(getattr(statement, "_for_update_arg", None) is not None for statement in executed_statements)


def test_delete_document_db_state_keeps_referenced_file_rows() -> None:
    db = _build_session(*_successful_delete_side_effects(remaining_refs=1))

    snapshot = DocumentService._delete_document_db_state(db, "doc-1")

    assert snapshot is not None
    assert snapshot.orphan_file_payloads == ()
    assert len(db.executed_statements) == 9


def test_delete_document_db_state_retries_transient_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("api.db.services.common_service.time.sleep", lambda _seconds: None)
    db = _build_session(_build_conflict(), *_successful_delete_side_effects())

    snapshot = DocumentService._delete_document_db_state(db, "doc-1")

    assert snapshot is not None
    db.rollback_mock.assert_called_once()


def test_remove_document_swallows_post_commit_cleanup_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = DeleteDocumentSnapshot(
        doc_id="doc-1",
        kb_id="kb-1",
        kb_name="Dataset A",
        tenant_id="tenant-1",
        thumbnail=None,
        location="doc-1.pdf",
        candidate_file_ids=("file-1",),
        orphan_file_payloads=(),
    )

    monkeypatch.setattr(DocumentService, "_delete_document_db_state", lambda _db, _doc_id: snapshot)
    monkeypatch.setattr(DocumentService, "_resolve_collection_name", lambda _tenant_id, _kb_name: "collection")
    monkeypatch.setattr(DocumentService, "delete_chunk_images", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("chunk cleanup failed")))
    monkeypatch.setattr(
        "api.db.services.task_service.cancel_all_task_of",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cancel failed")),
    )
    monkeypatch.setattr(
        DocMetadataService,
        "delete_document_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("metadata failed")),
    )

    class _ExplodingDocStore:
        def db_type(self) -> str:
            return "milvus"

        def has_collection(self, _collection_name: str) -> bool:
            raise RuntimeError("doc store unavailable")

        def search(self, *_args, **_kwargs):
            raise RuntimeError("graph search unavailable")

        def get_fields(self, *_args, **_kwargs):
            raise RuntimeError("graph fields unavailable")

    monkeypatch.setattr("api.db.services.document_service.settings.docStoreConn", _ExplodingDocStore())

    assert DocumentService.remove_document(_build_session(), Document(id="doc-1"), "tenant-x") is True


def test_dataset_delete_document_route_no_longer_runs_duplicate_db_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _build_session()
    user = type("User", (), {"id": "user-1"})()
    document = Document(id="doc-1")

    remove_document = MagicMock(return_value=True)
    file_delete = MagicMock()
    relation_delete = MagicMock()

    monkeypatch.setattr(dataset_app.FileService, "get_root_folder", lambda *_args, **_kwargs: type("Root", (), {"id": "root"})())
    monkeypatch.setattr(dataset_app.FileService, "init_knowledgebase_docs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dataset_app.DocumentService, "get_by_id", lambda *_args, **_kwargs: document)
    monkeypatch.setattr(dataset_app.DocumentService, "get_tenant_id", lambda *_args, **_kwargs: "tenant-1")
    monkeypatch.setattr(dataset_app.DocumentService, "remove_document", remove_document)
    monkeypatch.setattr(dataset_app.File2DocumentService, "get_storage_address", lambda *_args, **_kwargs: ("kb-1", "doc-1.pdf"))
    monkeypatch.setattr(dataset_app.FileService, "filter_delete", file_delete)
    monkeypatch.setattr(dataset_app.File2DocumentService, "delete_by_document_id", relation_delete)

    result = dataset_app.delete_document("kb-1", "doc-1", db=db, user=user)
    payload = json.loads(result.body)

    assert payload["code"] == RetCode.SUCCESS
    remove_document.assert_called_once_with(db, document, "tenant-1")
    file_delete.assert_not_called()
    relation_delete.assert_not_called()
