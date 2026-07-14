import importlib.util
import sys
import types
from contextlib import contextmanager
from pathlib import Path

from fastapi import BackgroundTasks

ROOT = Path(__file__).resolve().parents[2]


def _load_file2document_module(monkeypatch):
    class _FileType:
        DOC = types.SimpleNamespace(value="doc")
        FOLDER = types.SimpleNamespace(value="folder")

    @contextmanager
    def _db_connection():
        yield "worker-db"

    fake_api_apps = types.ModuleType("api.apps")
    fake_api_apps.__path__ = []
    fake_api_apps.manager = lambda: types.SimpleNamespace(id="user-1")

    fake_api_apps_services = types.ModuleType("api.apps.services")

    fake_file_convert_service = types.ModuleType("api.apps.services.file_convert_service")
    fake_file_convert_service.convert_files_with_new_session = lambda *_args: None

    fake_api_db = types.ModuleType("api.db")
    fake_api_db.FileType = _FileType

    fake_db_models = types.ModuleType("api.db.db_models")
    fake_db_models.db_connection = _db_connection
    fake_db_models.get_db = lambda: None

    fake_file2document_service = types.ModuleType("api.db.services.file2document_service")
    fake_file2document_service.File2DocumentService = type("File2DocumentService", (), {})

    fake_file_service = types.ModuleType("api.db.services.file_service")
    fake_file_service.FileService = type("FileService", (), {})

    fake_kb_service = types.ModuleType("api.db.services.knowledgebase_service")
    fake_kb_service.KnowledgebaseService = type("KnowledgebaseService", (), {})

    fake_doc_service = types.ModuleType("api.db.services.document_service")
    fake_doc_service.DocumentService = type("DocumentService", (), {})

    fake_api_utils = types.ModuleType("api.utils.api_utils")
    fake_api_utils.get_json_result = lambda data=None, **_: {"retcode": 0, "data": data}
    fake_api_utils.get_data_error_result = lambda retmsg, **_: {"retcode": 100, "retmsg": retmsg}
    fake_api_utils.server_error_response = lambda e: {"retcode": 500, "retmsg": repr(e)}

    fake_misc_utils = types.ModuleType("common.misc_utils")
    fake_misc_utils.get_uuid = lambda: "uuid"

    for name, module in {
        "api.apps": fake_api_apps,
        "api.apps.services": fake_api_apps_services,
        "api.apps.services.file_convert_service": fake_file_convert_service,
        "api.db": fake_api_db,
        "api.db.db_models": fake_db_models,
        "api.db.services.file2document_service": fake_file2document_service,
        "api.db.services.file_service": fake_file_service,
        "api.db.services.knowledgebase_service": fake_kb_service,
        "api.db.services.document_service": fake_doc_service,
        "api.utils.api_utils": fake_api_utils,
        "common.misc_utils": fake_misc_utils,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location(
        "file2document_app_subject",
        ROOT / "api/apps/file2document_app.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_file_convert_service_module(monkeypatch):
    @contextmanager
    def _db_connection():
        yield "worker-db"

    fake_db_models = types.ModuleType("api.db.db_models")
    fake_db_models.db_connection = _db_connection

    fake_file2document_service = types.ModuleType("api.db.services.file2document_service")
    fake_file2document_service.File2DocumentService = type("File2DocumentService", (), {})

    fake_file_service = types.ModuleType("api.db.services.file_service")
    fake_file_service.FileService = type("FileService", (), {})

    fake_kb_service = types.ModuleType("api.db.services.knowledgebase_service")
    fake_kb_service.KnowledgebaseService = type("KnowledgebaseService", (), {})

    fake_doc_service = types.ModuleType("api.db.services.document_service")
    fake_doc_service.DocumentService = type("DocumentService", (), {})

    fake_misc_utils = types.ModuleType("common.misc_utils")
    fake_misc_utils.get_uuid = lambda: "uuid"

    for name, module in {
        "api.db.db_models": fake_db_models,
        "api.db.services.file2document_service": fake_file2document_service,
        "api.db.services.file_service": fake_file_service,
        "api.db.services.knowledgebase_service": fake_kb_service,
        "api.db.services.document_service": fake_doc_service,
        "common.misc_utils": fake_misc_utils,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location(
        "file_convert_service_subject",
        ROOT / "api/apps/services/file_convert_service.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _file(**overrides):
    defaults = {
        "id": "f1",
        "type": "doc",
        "name": "demo.txt",
        "location": "demo.txt",
        "size": 10,
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def test_convert_validates_and_schedules_folder_work(monkeypatch):
    module = _load_file2document_module(monkeypatch)
    folder = _file(id="folder-1", type=module.FileType.FOLDER.value, name="folder")
    kb = types.SimpleNamespace(id="kb-1", parser_id="naive", pipeline_id="p1", parser_config={})

    monkeypatch.setattr(module.FileService, "get_by_ids", lambda _db, _ids: [folder], raising=False)
    monkeypatch.setattr(module.FileService, "get_all_innermost_file_ids", lambda _db, _fid, _acc: ["inner-1", "inner-2"], raising=False)
    monkeypatch.setattr(module.KnowledgebaseService, "get_by_id", lambda _db, _kb_id: kb, raising=False)

    background_tasks = BackgroundTasks()
    result = module.convert(["kb-1"], ["folder-1"], background_tasks, db="request-db", user=types.SimpleNamespace(id="user-1"))

    assert result == {"retcode": 0, "data": True}
    assert len(background_tasks.tasks) == 1
    task = background_tasks.tasks[0]
    assert task.func is module.convert_files_with_new_session
    assert task.args == (["inner-1", "inner-2"], ["kb-1"], "user-1")


def test_convert_rejects_missing_file_before_scheduling(monkeypatch):
    module = _load_file2document_module(monkeypatch)
    monkeypatch.setattr(module.FileService, "get_by_ids", lambda _db, _ids: [], raising=False)

    background_tasks = BackgroundTasks()
    result = module.convert(["kb-1"], ["missing"], background_tasks, db="request-db", user=types.SimpleNamespace(id="user-1"))

    assert result["retmsg"] == "File not found!"
    assert background_tasks.tasks == []


def test_convert_worker_removes_old_links_and_inserts_new_docs(monkeypatch):
    module = _load_file_convert_service_module(monkeypatch)
    events = []
    old_doc = types.SimpleNamespace(id="doc-old")
    file = _file(id="f1", name="demo.txt")
    kb = types.SimpleNamespace(id="kb-1", parser_id="naive", pipeline_id="p1", parser_config={"chunk": 1})

    monkeypatch.setattr(module.File2DocumentService, "get_by_file_id", lambda _db, _fid: [types.SimpleNamespace(document_id="doc-old")], raising=False)
    monkeypatch.setattr(module.DocumentService, "get_by_id", lambda _db, _doc_id: old_doc, raising=False)
    monkeypatch.setattr(module.DocumentService, "get_tenant_id", lambda _db, _doc_id: "tenant-1", raising=False)
    monkeypatch.setattr(module.DocumentService, "remove_document", lambda *_args: events.append("remove-doc") or True, raising=False)
    monkeypatch.setattr(module.File2DocumentService, "delete_by_file_id", lambda _db, fid: events.append(("delete-link", fid)), raising=False)
    monkeypatch.setattr(module.FileService, "get_by_id", lambda _db, _fid: file, raising=False)
    monkeypatch.setattr(module.KnowledgebaseService, "get_by_id", lambda _db, _kb_id: kb, raising=False)
    monkeypatch.setattr(module.FileService, "get_parser", lambda *_args: "picked-parser", raising=False)

    inserted_doc = {}
    monkeypatch.setattr(
        module.DocumentService,
        "insert",
        lambda _db, payload: inserted_doc.update(payload) or types.SimpleNamespace(id="doc-new"),
        raising=False,
    )
    monkeypatch.setattr(
        module.File2DocumentService,
        "insert",
        lambda _db, payload: events.append(("insert-link", payload)),
        raising=False,
    )

    module.convert_files("worker-db", ["f1"], ["kb-1"], "user-1")

    assert events[0] == "remove-doc"
    assert ("delete-link", "f1") in events
    assert inserted_doc["created_by"] == "user-1"
    assert inserted_doc["parser_id"] == "picked-parser"
    assert inserted_doc["pipeline_id"] == "p1"
    assert ("insert-link", {"id": "uuid", "file_id": "f1", "document_id": "doc-new"}) in events
