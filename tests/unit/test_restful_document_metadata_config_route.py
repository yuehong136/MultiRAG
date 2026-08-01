"""document RESTful 元数据配置路由契约测试。

PUT /api/v1/datasets/{dataset_id}/documents/{document_id}/metadata/config 收编了
旧的 POST /v1/document/update_metadata_setting（后者标 deprecated 留旧）。

三条钉板：
- **数组载荷**：前端保存元数据模板时发的是字段定义**数组**，只收 dict 会 422 ——
  旧端点正是栽在这上面，新端点数组/对象两种形状都收；
- **归属校验**：文档必须属于路径里的数据集，拿任一自有数据集改不了他人文档的配置；
- **写入面**：落到 parser_config 的 ``metadata`` 键，不是 meta_fields。
"""

import sys
from types import SimpleNamespace

from sqlalchemy.orm import Session

from api.apps.services import document_api_service
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.constants import RetCode

_PATH = "/api/v1/datasets/kb1/documents/doc1/metadata/config"
_SETTINGS = [{"key": "author", "type": "string", "description": "作者", "enum": ["alice"]}]


def _route_module():
    return sys.modules["api.apps.restful_apis.document"]


def _assert_sync_facade(sessions):
    assert sessions
    for s in sessions:
        assert isinstance(s, Session), f"同步 service 收到 {type(s).__name__}，应为 sqlalchemy.orm.Session"


def _stub_chain(monkeypatch, sessions, *, can_update=True, docs=(SimpleNamespace(id="doc1"),)):
    config_calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kb_id: sessions.append(s) or SimpleNamespace(id=kb_id, tenant_id="tenant-unit")))
    monkeypatch.setattr(document_api_service, "can_update_dataset", lambda s, user_id, kb: sessions.append(s) or can_update)
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: sessions.append(s) or list(docs)))

    def _update_parser_config(cls, s, doc_id, config):
        sessions.append(s)
        config_calls.append((doc_id, config))

    monkeypatch.setattr(DocumentService, "update_parser_config", classmethod(_update_parser_config))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda cls, s, doc_id: sessions.append(s) or SimpleNamespace(id=doc_id)))
    monkeypatch.setattr(document_api_service, "map_doc_keys", lambda s, doc: {"id": doc.id, "dataset_id": "kb1"})
    return config_calls


def test_update_accepts_the_settings_array_the_frontend_sends(client, monkeypatch):
    sessions: list[object] = []
    config_calls = _stub_chain(monkeypatch, sessions)

    resp = client.put(_PATH, json={"metadata": _SETTINGS})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == RetCode.SUCCESS, body
    assert body["data"] == {"id": "doc1", "dataset_id": "kb1"}, body
    assert config_calls == [("doc1", {"metadata": _SETTINGS})], config_calls
    _assert_sync_facade(sessions)


def test_update_accepts_json_schema_object_shape(client, monkeypatch):
    sessions: list[object] = []
    config_calls = _stub_chain(monkeypatch, sessions)
    schema = {"type": "object", "properties": {"author": {"type": "string"}}}

    resp = client.put(_PATH, json={"metadata": schema})

    assert resp.status_code == 200, resp.text
    assert config_calls == [("doc1", {"metadata": schema})], config_calls


def test_update_rejects_scalar_metadata_and_missing_body(client, monkeypatch):
    sessions: list[object] = []
    _stub_chain(monkeypatch, sessions)

    assert client.put(_PATH, json={"metadata": "author"}).status_code == 422
    assert client.put(_PATH, json={}).status_code == 422


def test_update_requires_the_document_to_live_in_the_dataset(client, monkeypatch):
    sessions: list[object] = []
    _stub_chain(monkeypatch, sessions, docs=())

    resp = client.put(_PATH, json={"metadata": _SETTINGS})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == RetCode.DATA_ERROR, body
    assert "not found in dataset" in body["message"], body


def test_update_rejects_members_without_update_permission(client, monkeypatch):
    sessions: list[object] = []
    _stub_chain(monkeypatch, sessions, can_update=False)

    resp = client.put(_PATH, json={"metadata": _SETTINGS})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == RetCode.AUTHENTICATION_ERROR, body


def test_update_reports_missing_dataset(client, monkeypatch):
    sessions: list[object] = []
    _stub_chain(monkeypatch, sessions)
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kb_id: sessions.append(s) or None))

    resp = client.put(_PATH, json={"metadata": _SETTINGS})

    body = resp.json()
    assert body["code"] == RetCode.DATA_ERROR, body
    assert "You don't own the dataset" in body["message"], body


def test_deprecated_web_route_also_accepts_the_settings_array(client, monkeypatch):
    """旧端点的 422 断链同批修好——前端全量迁移前它仍是活的消费面。"""
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(DocumentService, "accessible", classmethod(lambda cls, s, doc_id, user_id: True))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda cls, s, doc_id: SimpleNamespace(id=doc_id)))
    monkeypatch.setattr(DocumentService, "update_parser_config", classmethod(lambda cls, s, doc_id, config: calls.append((doc_id, config))))
    monkeypatch.setattr(DocumentService, "serialize_document", classmethod(lambda cls, s, doc: {"id": doc.id}))

    resp = client.post("/v1/document/update_metadata_setting", json={"doc_id": "doc1", "metadata": _SETTINGS})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retcode"] == RetCode.SUCCESS, body
    assert calls == [("doc1", {"metadata": _SETTINGS})], calls
