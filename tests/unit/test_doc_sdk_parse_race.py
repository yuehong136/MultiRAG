import json
from types import SimpleNamespace
from unittest.mock import Mock

from api.apps.sdk import doc as doc_routes
from common.constants import RetCode, TaskStatus


def _response_json(response):
    return json.loads(response.body)


def test_parse_documents_stops_when_atomic_running_update_loses(monkeypatch):
    db = object()
    existing_doc = SimpleNamespace(id="doc-1", status="1", run=TaskStatus.UNSTART.value)
    docstore_delete = Mock()
    task_delete = Mock()
    queue_tasks = Mock()

    monkeypatch.setattr(doc_routes.KnowledgebaseService, "query", lambda *_args, **_kwargs: [object()])
    monkeypatch.setattr(doc_routes.DocumentService, "query", lambda *_args, **_kwargs: [existing_doc])
    monkeypatch.setattr(doc_routes.DocumentService, "filter_update", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(doc_routes.settings, "docStoreConn", SimpleNamespace(delete=docstore_delete))
    monkeypatch.setattr(doc_routes.TaskService, "filter_delete", task_delete)
    monkeypatch.setattr(doc_routes, "queue_tasks", queue_tasks)

    response = doc_routes.parse_documents(
        "kb-1",
        doc_routes.ParseDocumentRequest(document_ids=["doc-1"]),
        db=db,
        tenant_id="tenant-1",
    )

    body = _response_json(response)
    assert body["code"] == RetCode.DATA_ERROR
    assert body["message"] == "Can't parse document that is currently being processed"
    docstore_delete.assert_not_called()
    task_delete.assert_not_called()
    queue_tasks.assert_not_called()


def test_parse_documents_uses_atomic_update_before_queueing(monkeypatch):
    db = object()
    existing_doc = SimpleNamespace(id="doc-1", status="1", run=TaskStatus.UNSTART.value)
    refreshed_doc = SimpleNamespace(
        to_dict=lambda: {
            "id": "doc-1",
            "kb_id": "kb-1",
            "name": "doc.pdf",
            "type": "pdf",
            "parser_id": "naive",
            "parser_config": {},
        }
    )
    filter_update_calls = []
    docstore_delete = Mock()
    task_delete = Mock()
    queue_tasks = Mock()

    def filter_update(_db, filters, update_data):
        filter_update_calls.append((filters, update_data.copy()))
        return True

    monkeypatch.setattr(doc_routes.KnowledgebaseService, "query", lambda *_args, **_kwargs: [object()])
    monkeypatch.setattr(doc_routes.DocumentService, "query", lambda *_args, **_kwargs: [existing_doc])
    monkeypatch.setattr(doc_routes.DocumentService, "filter_update", filter_update)
    monkeypatch.setattr(doc_routes.DocumentService, "get_by_id", lambda *_args, **_kwargs: refreshed_doc)
    monkeypatch.setattr(doc_routes.File2DocumentService, "get_storage_address", lambda *_args, **_kwargs: ("bucket-1", "doc.pdf"))
    monkeypatch.setattr(doc_routes.settings, "docStoreConn", SimpleNamespace(delete=docstore_delete))
    monkeypatch.setattr(doc_routes.search, "index_name", lambda tenant_id: ["idx", tenant_id])
    monkeypatch.setattr(doc_routes.TaskService, "filter_delete", task_delete)
    monkeypatch.setattr(doc_routes, "queue_tasks", queue_tasks)

    response = doc_routes.parse_documents(
        "kb-1",
        doc_routes.ParseDocumentRequest(document_ids=["doc-1"]),
        db=db,
        tenant_id="tenant-1",
    )

    assert _response_json(response)["code"] == RetCode.SUCCESS
    assert filter_update_calls
    _, update_data = filter_update_calls[0]
    assert update_data == {
        "progress": 0,
        "progress_msg": "",
        "run": TaskStatus.RUNNING.value,
        "chunk_num": 0,
        "token_num": 0,
    }
    docstore_delete.assert_called_once_with({"doc_id": "doc-1"}, ["idx", "tenant-1"], "kb-1")
    task_delete.assert_called_once()
    queued_doc = queue_tasks.call_args.args[1]
    assert queued_doc["id"] == "doc-1"
    assert queued_doc["tenant_id"] == "tenant-1"
    queue_tasks.assert_called_once_with(db, queued_doc, "bucket-1", "doc.pdf", 0)
