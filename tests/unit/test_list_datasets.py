"""
Dataset RESTful service 层单元测试 —— list_datasets
对齐 ragflow test_dataset_mangement/test_list_datasets.py 场景，
覆盖 owner_ids / keywords / parser_id 承接与 nickname/tenant_avatar 补全。
共享 fixtures（db）见 tests/unit/conftest.py。
"""

import types

import api.apps.services.dataset_api_service as svc


def _patch_list(monkeypatch, kbs, capture):
    monkeypatch.setattr(svc.KnowledgebaseService, "get_kb_by_id", lambda d, kb_id, tid: True)
    monkeypatch.setattr(svc.KnowledgebaseService, "get_kb_by_name", lambda d, name, tid: True)
    monkeypatch.setattr(svc.TenantService, "get_joined_tenants_by_user_id", lambda d, tid: [types.SimpleNamespace(tenant_id=tid)])

    def fake_get_list(d, joined_tenant_ids, user_id, page, page_size, orderby, desc, _id, name, keywords=None, parser_id=None):
        capture.update(joined_tenant_ids=joined_tenant_ids, keywords=keywords, parser_id=parser_id)
        return kbs, len(kbs)

    monkeypatch.setattr(svc.KnowledgebaseService, "get_list", fake_get_list)
    monkeypatch.setattr(svc, "remap_dictionary_keys", lambda d: d)


def test_list_basic_with_nickname(monkeypatch, db):
    cap = {}
    kbs = [{"id": "k1", "tenant_id": "t1"}]
    _patch_list(monkeypatch, kbs, cap)
    user = types.SimpleNamespace(id="t1", to_dict=lambda: {"nickname": "alice", "avatar": "av"})
    monkeypatch.setattr(svc.UserService, "get_by_ids", lambda d, ids: [user])

    ok, res = svc.list_datasets(db, "t1", {"page": 1, "page_size": 30})
    assert ok is True
    assert res["total"] == 1
    row = res["data"][0]
    assert row["nickname"] == "alice"
    assert row["tenant_avatar"] == "av"


def test_list_forwards_keywords_and_parser_id(monkeypatch, db):
    cap = {}
    _patch_list(monkeypatch, [], cap)
    monkeypatch.setattr(svc.UserService, "get_by_ids", lambda d, ids: [])

    ok, res = svc.list_datasets(db, "t1", {"keywords": "foo", "parser_id": "qa"})
    assert ok is True
    assert cap["keywords"] == "foo"
    assert cap["parser_id"] == "qa"


def test_list_owner_ids_override_scope(monkeypatch, db):
    cap = {}
    _patch_list(monkeypatch, [], cap)
    monkeypatch.setattr(svc.UserService, "get_by_ids", lambda d, ids: [])

    ok, _ = svc.list_datasets(db, "t1", {"owner_ids": ["tA", "tB"]})
    assert ok is True
    # owner_ids 指定时作为 get_list 的 joined_tenant_ids
    assert cap["joined_tenant_ids"] == ["tA", "tB"]


def test_list_permission_error_on_unknown_id(monkeypatch, db):
    monkeypatch.setattr(svc.KnowledgebaseService, "get_kb_by_id", lambda d, kb_id, tid: None)
    ok, msg = svc.list_datasets(db, "t1", {"id": "nope"})
    assert ok is False
    assert "lacks permission" in msg
