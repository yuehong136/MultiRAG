"""Batch deletion must report incomplete work and continue independent items."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from api.apps.services import file_api_service as svc


@pytest.fixture
def batch(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    files: dict[str, Any] = {}
    links: dict[str, list[Any]] = {}
    storage = MagicMock()
    storage.rm.return_value = None
    delete = MagicMock(side_effect=lambda db, fid: int(files.pop(fid, None) is not None))
    monkeypatch.setattr(svc.FileService, "get_by_id", lambda db, fid: files.get(fid))
    monkeypatch.setattr(svc.FileService, "list_all_files_by_parent_id", lambda db, fid: [f for f in files.values() if f.parent_id == fid])
    monkeypatch.setattr(svc.FileService, "delete_by_id", delete)
    monkeypatch.setattr(svc.File2DocumentService, "get_by_file_id", lambda db, fid: links.get(fid, []))
    monkeypatch.setattr(svc.File2DocumentService, "delete_by_file_id", lambda db, fid: links.pop(fid, None))
    monkeypatch.setattr(svc, "check_file_team_permission", lambda db, f, uid: f.tenant_id == uid)
    monkeypatch.setattr(svc.settings, "STORAGE_IMPL", storage)

    def add(fid: str, parent: str = "root", **kwargs: Any) -> Any:
        f = SimpleNamespace(id=fid, parent_id=parent, tenant_id="owner", source_type=None, location=fid, type="doc")
        for key, value in kwargs.items():
            setattr(f, key, value)
        files[fid] = f
        return f

    return SimpleNamespace(files=files, links=links, storage=storage, delete=delete, add=add)


def test_mixed_ids_continue_and_duplicates_count_once(batch: SimpleNamespace, db: Session) -> None:
    batch.add("denied", tenant_id="other")
    batch.add("good")
    batch.add("no-tenant", tenant_id=None)
    ok, result = svc.delete_files(db, "owner", ["missing", "denied", "good", "good", "no-tenant"])
    assert not ok and result["success_count"] == 1 and len(result["errors"]) == 3
    assert set(batch.files) == {"denied", "no-tenant"}
    batch.storage.rm.assert_called_once_with("root", "good")


def test_recursive_overlap_counts_once(batch: SimpleNamespace, db: Session) -> None:
    batch.add("folder", type="folder")
    batch.add("nested", "folder", type="folder")
    batch.add("leaf", "nested")
    assert svc.delete_files(db, "owner", ["folder", "leaf", "nested", "folder"]) == (True, {"success_count": 3, "errors": []})
    assert not batch.files


@pytest.mark.parametrize("failure", ["storage", "false", "database", "zero", "permission", "knowledgebase", "cycle"])
def test_failed_child_keeps_ancestors_and_deletes_siblings(batch: SimpleNamespace, db: Session, failure: str) -> None:
    batch.add("folder", type="folder")
    bad = batch.add("bad", "folder")
    batch.add("good", "folder")
    if failure == "storage":
        batch.storage.rm.side_effect = lambda b, key: (_ for _ in ()).throw(OSError("secret")) if key == "bad" else None
    elif failure == "false":
        batch.storage.rm.side_effect = lambda b, key: key != "bad"
    elif failure in {"database", "zero"}:

        def delete(db: Session, fid: str) -> int:
            if fid == "bad":
                if failure == "database":
                    raise RuntimeError("secret")
                return 0
            return int(batch.files.pop(fid, None) is not None)

        batch.delete.side_effect = delete
    elif failure == "permission":
        bad.tenant_id = "other"
    elif failure == "knowledgebase":
        bad.source_type = svc.FileSource.KNOWLEDGEBASE
    else:
        bad.type = "folder"
        batch.files["folder"].parent_id = "bad"
    ok, result = svc.delete_files(db, "owner", ["folder"])
    assert not ok and result["success_count"] == 1
    assert set(batch.files) == {"folder", "bad"}
    assert "secret" not in str(result)


@pytest.mark.parametrize("failure", ["missing", "denied", "tenant", "remove", "relations"])
def test_linked_document_failure_not_success(batch: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    batch.add("bad")
    batch.add("good")
    batch.links["bad"] = [SimpleNamespace(document_id="doc")]
    monkeypatch.setattr(svc.DocumentService, "get_by_id", lambda db, did: None if failure == "missing" else SimpleNamespace(id=did, kb_id="kb"))
    monkeypatch.setattr(svc.KnowledgebaseService, "get_by_id", lambda *args: object())
    monkeypatch.setattr(svc, "check_kb_team_permission", lambda *args: failure != "denied")
    monkeypatch.setattr(svc.DocumentService, "get_tenant_id", lambda *args: None if failure == "tenant" else "owner")
    monkeypatch.setattr(svc.DocumentService, "remove_document", lambda *args, **kwargs: failure != "remove")
    if failure == "relations":
        monkeypatch.setattr(svc.File2DocumentService, "delete_by_file_id", lambda *args: 0)
    ok, result = svc.delete_files(db, "owner", ["bad", "good"])
    assert not ok and result["success_count"] == 1
    assert set(batch.files) == {"bad"}
    assert len(result["errors"]) == 1
    if failure in {"missing", "denied"}:
        batch.storage.rm.assert_called_once_with("root", "good")


def test_all_linked_documents_authorized_before_first_write(batch: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    batch.add("shared")
    batch.links["shared"] = [SimpleNamespace(document_id="allowed"), SimpleNamespace(document_id="denied")]
    monkeypatch.setattr(svc.DocumentService, "get_by_id", lambda db, did: SimpleNamespace(id=did, kb_id=did))
    monkeypatch.setattr(svc.KnowledgebaseService, "get_by_id", lambda db, kid: kid)
    monkeypatch.setattr(svc, "check_kb_team_permission", lambda db, kb, uid: kb == "allowed")
    remove = MagicMock()
    monkeypatch.setattr(svc.DocumentService, "remove_document", remove)
    ok, result = svc.delete_files(db, "owner", ["shared"])
    assert not ok and result["success_count"] == 0
    batch.storage.rm.assert_not_called()
    remove.assert_not_called()
    batch.delete.assert_not_called()


@pytest.fixture(autouse=True)
def ordinary_file_skill_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing fixtures model ordinary files; managed-tree SQL is tested separately."""
    monkeypatch.setattr(svc, "is_skill_managed", lambda *args, **kwargs: False)
    monkeypatch.setattr(svc, "is_python_core", lambda *args, **kwargs: False)
