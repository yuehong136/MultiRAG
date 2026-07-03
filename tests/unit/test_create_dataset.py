"""
Dataset RESTful service 层单元测试 —— create_dataset
对齐 ragflow test_dataset_mangement/test_create_dataset.py 的场景，
改写为针对 api/apps/services/dataset_api_service.create_dataset 的 mock 单测。
共享 fixtures（db / fake_kb）见 tests/unit/conftest.py。
"""

import api.apps.services.dataset_api_service as svc


def _patch_common(monkeypatch, fake_kb):
    monkeypatch.setattr(svc, "get_parser_config", lambda pid, pc: {"chunk_token_num": 128})
    monkeypatch.setattr(svc, "ensure_tenant_model_id_for_params", lambda d, tid, p: p)
    monkeypatch.setattr(svc, "remap_dictionary_keys", lambda d: d)
    monkeypatch.setattr(svc.KnowledgebaseService, "save", lambda d, **p: True)
    monkeypatch.setattr(svc.KnowledgebaseService, "get_by_id", lambda d, _id: fake_kb(id=_id))


def test_create_dataset_success(monkeypatch, db, fake_kb):
    _patch_common(monkeypatch, fake_kb)
    monkeypatch.setattr(svc.KnowledgebaseService, "get_or_none", lambda d, **kw: None)
    captured = {}

    # 注意：service 以关键字 db=db 调用 create_with_name，故首参须命名为 db
    def fake_create(db, *, name, tenant_id, parser_id=None, embd_id=None, parser_config=None, **kwargs):
        captured.update(name=name, parser_id=parser_id, embd_id=embd_id, kwargs=kwargs)
        return True, {"id": "kb1", "embd_id": embd_id or "bge@builtin"}

    monkeypatch.setattr(svc.KnowledgebaseService, "create_with_name", fake_create)

    ok, data = svc.create_dataset(db, "t1", {"name": "ds", "chunk_method": "naive", "avatar": "a", "ext": {}})
    assert ok is True, data
    assert captured["name"] == "ds"
    assert captured["parser_id"] == "naive"
    # avatar 等剩余字段以 kwargs 形式透传给 create_with_name
    assert captured["kwargs"].get("avatar") == "a"


def test_create_dataset_duplicate_name(monkeypatch, db, fake_kb):
    _patch_common(monkeypatch, fake_kb)
    monkeypatch.setattr(svc.KnowledgebaseService, "get_or_none", lambda d, **kw: fake_kb(name="ds"))
    ok, msg = svc.create_dataset(db, "t1", {"name": "ds", "chunk_method": "naive", "ext": {}})
    assert ok is False
    assert "already exists" in msg


def test_create_dataset_ext_passthrough(monkeypatch, db, fake_kb):
    """ext 里的旧 web 参数应合并进 req 并透传给 create_with_name（对标 4bb1acaa5）。"""
    _patch_common(monkeypatch, fake_kb)
    monkeypatch.setattr(svc.KnowledgebaseService, "get_or_none", lambda d, **kw: None)
    captured = {}

    def fake_create(db, *, name, tenant_id, parser_id=None, embd_id=None, parser_config=None, **kwargs):
        captured.update(kwargs=kwargs)
        return True, {"id": "kb1", "embd_id": "bge@builtin"}

    monkeypatch.setattr(svc.KnowledgebaseService, "create_with_name", fake_create)
    ok, _ = svc.create_dataset(db, "t1", {"name": "ds", "chunk_method": "naive", "ext": {"language": "Chinese"}})
    assert ok is True
    assert captured["kwargs"].get("language") == "Chinese"


def test_create_dataset_tag_guard_infinity(monkeypatch, db, fake_kb):
    """Infinity 引擎下禁用 tag chunk_method（承接旧 web 守卫）。"""
    _patch_common(monkeypatch, fake_kb)
    monkeypatch.setattr(svc.settings, "DOC_ENGINE_INFINITY", True)
    monkeypatch.setattr(svc.KnowledgebaseService, "get_or_none", lambda d, **kw: None)
    ok, msg = svc.create_dataset(db, "t1", {"name": "ds", "chunk_method": "tag", "ext": {}})
    assert ok is False
    assert "Infinity" in msg


def test_create_dataset_embedding_unavailable(monkeypatch, db, fake_kb):
    """显式 embedding_model 校验失败时返回错误信息字符串（已解耦 HTTP）。"""
    _patch_common(monkeypatch, fake_kb)
    monkeypatch.setattr(svc.KnowledgebaseService, "get_or_none", lambda d, **kw: None)
    monkeypatch.setattr(svc.KnowledgebaseService, "create_with_name", lambda db, **kw: (True, {"id": "kb1", "embd_id": "x@y"}))
    monkeypatch.setattr(svc, "verify_embedding_availability", lambda d, embd, tid: (False, "Unsupported model: <x@y>"))
    ok, msg = svc.create_dataset(db, "t1", {"name": "ds", "chunk_method": "naive", "embedding_model": "x@y", "ext": {}})
    assert ok is False
    assert "Unsupported model" in msg
