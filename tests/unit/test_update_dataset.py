"""
Dataset RESTful service 层单元测试 —— update_dataset
对齐 ragflow test_dataset_mangement/test_update_dataset.py 场景，
覆盖 connectors / pipeline 切换 / folder 重命名 / embedding 约束 / tag 守卫。
共享 fixtures（db / fake_kb）见 tests/unit/conftest.py。
"""

import api.apps.services.dataset_api_service as svc


def _patch_update_common(monkeypatch, kb, captured_update):
    # 第一处 get_or_none 取 kb（带 id），第二处取重名（带 name）→ 返回 None 表示无冲突
    def fake_get_or_none(d, **kw):
        if "id" in kw:
            return kb
        return None

    monkeypatch.setattr(svc.KnowledgebaseService, "get_or_none", fake_get_or_none)
    monkeypatch.setattr(svc, "ensure_tenant_model_id_for_params", lambda d, tid, p: p)
    monkeypatch.setattr(svc, "remap_dictionary_keys", lambda d: d)
    monkeypatch.setattr(svc, "deep_merge", lambda a, b: {**(a or {}), **(b or {})})
    monkeypatch.setattr(svc, "get_parser_config", lambda pid, pc: {"x": 1})

    def fake_update_by_id(d, kb_id, payload):
        captured_update.update(payload)
        return True

    monkeypatch.setattr(svc.KnowledgebaseService, "update_by_id", fake_update_by_id)
    monkeypatch.setattr(svc.KnowledgebaseService, "get_by_id", lambda d, kb_id: kb)


def test_update_no_props(db):
    ok, msg = svc.update_dataset(db, "t1", "kb1", {})
    assert ok is False
    assert "No properties" in msg


def test_update_permission_error(monkeypatch, db):
    monkeypatch.setattr(svc.KnowledgebaseService, "get_or_none", lambda d, **kw: None)
    ok, msg = svc.update_dataset(db, "t1", "kb1", {"description": "x"})
    assert ok is False
    assert "lacks permission" in msg


def test_update_success_and_name_rename_folder(monkeypatch, db, fake_kb):
    kb = fake_kb(name="old")
    cap = {}
    _patch_update_common(monkeypatch, kb, cap)
    renamed = []
    monkeypatch.setattr(svc.FileService, "filter_update", lambda d, flt, data: renamed.append(data))

    ok, data = svc.update_dataset(db, "t1", "kb1", {"name": "new"})
    assert ok is True
    assert cap.get("name") == "new"
    # 改名时同步重命名 folder（承接旧 web）
    assert renamed and renamed[0] == {"name": "new"}


def test_update_duplicate_name(monkeypatch, db, fake_kb):
    kb = fake_kb(name="old")
    cap = {}
    _patch_update_common(monkeypatch, kb, cap)
    # 重名冲突：第二处 get_or_none(name=...) 返回已存在
    monkeypatch.setattr(svc.KnowledgebaseService, "get_or_none", lambda d, **kw: kb if "id" in kw else fake_kb(name="new"))
    ok, msg = svc.update_dataset(db, "t1", "kb1", {"name": "new"})
    assert ok is False
    assert "already exists" in msg


def test_update_embedding_locked_when_chunks_exist(monkeypatch, db, fake_kb):
    kb = fake_kb(embd_id="old@f", chunk_num=5)
    cap = {}
    _patch_update_common(monkeypatch, kb, cap)
    ok, msg = svc.update_dataset(db, "t1", "kb1", {"embedding_model": "new@f"})
    assert ok is False
    assert "must remain" in msg


def test_update_pipeline_shift_clears_pipeline_id(monkeypatch, db, fake_kb):
    """从 pipeline dataset 切回普通 parser 时清空 pipeline_id（对标 8d4a3d0d）。"""
    kb = fake_kb(pipeline_id="p" * 32, parser_id="naive")
    cap = {}
    _patch_update_common(monkeypatch, kb, cap)
    ok, _ = svc.update_dataset(db, "t1", "kb1", {"chunk_method": "qa"})
    assert ok is True
    assert cap.get("pipeline_id") == ""


def test_update_links_connectors_only_when_provided(monkeypatch, db, fake_kb):
    kb = fake_kb()
    cap = {}
    _patch_update_common(monkeypatch, kb, cap)
    linked = []
    monkeypatch.setattr(svc.Connector2KbService, "link_connectors", lambda d, kb_id, conns, tid: linked.append(conns) or "")

    ok, data = svc.update_dataset(db, "t1", "kb1", {"description": "x", "connectors": [{"id": "c1"}]})
    assert ok is True
    assert linked == [[{"id": "c1"}]]
    assert data["connectors"] == [{"id": "c1"}]


def test_update_no_connector_link_when_absent(monkeypatch, db, fake_kb):
    kb = fake_kb()
    cap = {}
    _patch_update_common(monkeypatch, kb, cap)
    linked = []
    monkeypatch.setattr(svc.Connector2KbService, "link_connectors", lambda d, kb_id, conns, tid: linked.append(conns) or "")

    ok, _ = svc.update_dataset(db, "t1", "kb1", {"description": "x"})
    assert ok is True
    # 未显式传 connectors → 不调用 link，避免误清空已有关联
    assert linked == []


def test_update_tag_guard_infinity(monkeypatch, db, fake_kb):
    kb = fake_kb()
    cap = {}
    _patch_update_common(monkeypatch, kb, cap)
    monkeypatch.setattr(svc.settings, "DOC_ENGINE_INFINITY", True)
    ok, msg = svc.update_dataset(db, "t1", "kb1", {"chunk_method": "tag"})
    assert ok is False
    assert "Infinity" in msg
