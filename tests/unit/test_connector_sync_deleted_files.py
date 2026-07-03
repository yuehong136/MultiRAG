from types import SimpleNamespace

from sqlalchemy.orm import Session

from api.db.services import connector_service as connector_module
from api.db.services.connector_service import ConnectorService
from api.utils.common import hash128


def test_cleanup_stale_documents_deletes_docs_missing_from_source(monkeypatch):
    db = Session()
    connector_id = "connector-1"
    kb_id = "kb-1"
    tenant_id = "tenant-1"
    retained_source_id = "github-doc-1"
    retained_doc_id = hash128(retained_source_id)
    stale_doc_ids = ["stale-doc-1", "stale-doc-2"]
    delete_calls = []
    increase_calls = []
    list_calls = 0

    def fake_list_doc_headers(_db, _kb_id, _source_type):
        nonlocal list_calls
        list_calls += 1
        if list_calls == 1:
            return [
                {"id": retained_doc_id, "kb_id": kb_id, "source_type": _source_type, "name": "keep"},
                {"id": stale_doc_ids[0], "kb_id": kb_id, "source_type": _source_type, "name": "stale-1"},
                {"id": stale_doc_ids[1], "kb_id": kb_id, "source_type": _source_type, "name": "stale-2"},
            ]
        return [
            {"id": retained_doc_id, "kb_id": kb_id, "source_type": _source_type, "name": "keep"},
        ]

    def fake_delete_docs(_db, doc_ids, _tenant_id):
        delete_calls.append(list(doc_ids))
        return ""

    def fake_increase_removed_docs(_db, task_id, removed_count, err_msg="", error_count=0):
        increase_calls.append((task_id, removed_count, err_msg, error_count))

    monkeypatch.setattr(
        connector_module.Connector2KbService,
        "query",
        staticmethod(lambda _db, **_kwargs: [SimpleNamespace()]),
    )
    monkeypatch.setattr(
        connector_module.ConnectorService,
        "get_by_id",
        staticmethod(lambda _db, _connector_id: SimpleNamespace(id=connector_id, source="github")),
    )
    monkeypatch.setattr(
        connector_module.DocumentService,
        "list_doc_headers_by_kb_and_source_type",
        staticmethod(fake_list_doc_headers),
    )
    monkeypatch.setattr(
        connector_module.FileService,
        "delete_docs",
        staticmethod(fake_delete_docs),
    )
    monkeypatch.setattr(
        connector_module.SyncLogsService,
        "increase_removed_docs",
        staticmethod(fake_increase_removed_docs),
    )

    removed_count, errors = ConnectorService.cleanup_stale_documents_for_task(
        db,
        "task-1",
        connector_id,
        kb_id,
        tenant_id,
        [SimpleNamespace(id=retained_source_id)],
        delete_batch_size=1,
    )

    assert removed_count == 2
    assert errors == []
    assert delete_calls == [["stale-doc-1"], ["stale-doc-2"]]
    assert increase_calls == [("task-1", 2, "", 0)]
