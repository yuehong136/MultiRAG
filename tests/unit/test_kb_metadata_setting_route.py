"""知识库元数据模板路由契约测试（POST /v1/kb/update_metadata_setting）。

两条钉板，对应本路由此前的两个真实缺陷：
- **数组载荷**：前端保存模板发的是字段定义数组，路由原先只收 dict，每次保存都 422
  （读侧 ``settingsToTableData`` 对非数组返回 []，所以界面只表现为"保存后没变化"）；
- **越权**：原先只要登录就能改**任意**知识库的元数据模板，没有任何归属校验。

与文档级 ``PUT /api/v1/datasets/{id}/documents/{id}/metadata/config`` 是同一组契约，
见 ``test_restful_document_metadata_config_route.py``。
"""

from types import SimpleNamespace

from api.db.services.knowledgebase_service import KnowledgebaseService
from common.constants import RetCode

_PATH = "/v1/kb/update_metadata_setting"
_SETTINGS = [{"key": "author", "type": "string", "description": "作者", "enum": ["alice"]}]


def _stub_kb(monkeypatch, *, accessible=True, parser_config=None, exists=True):
    updates: list[tuple[str, dict]] = []

    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, user_id: accessible))

    def _get_by_id(cls, _s, kb_id):
        if not exists:
            return None
        return SimpleNamespace(id=kb_id, to_dict=lambda: {"id": kb_id, "parser_config": parser_config})

    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(_get_by_id))
    monkeypatch.setattr(KnowledgebaseService, "update_by_id", classmethod(lambda cls, _s, kb_id, payload: updates.append((kb_id, payload)) or True))
    return updates


def test_accepts_the_settings_array_the_frontend_sends(client, monkeypatch):
    updates = _stub_kb(monkeypatch, parser_config={"chunk_token_num": 128})

    resp = client.post(_PATH, json={"kb_id": "kb1", "metadata": _SETTINGS})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retcode"] == RetCode.SUCCESS, body
    assert updates == [("kb1", {"parser_config": {"chunk_token_num": 128, "metadata": _SETTINGS, "enable_metadata": True}})], updates


def test_accepts_json_schema_object_shape_and_null_parser_config(client, monkeypatch):
    updates = _stub_kb(monkeypatch, parser_config=None)
    schema = {"type": "object", "properties": {"author": {"type": "string"}}}

    resp = client.post(_PATH, json={"kb_id": "kb1", "metadata": schema, "enable_metadata": False})

    assert resp.status_code == 200, resp.text
    assert updates == [("kb1", {"parser_config": {"metadata": schema, "enable_metadata": False}})], updates


def test_rejects_scalar_metadata(client, monkeypatch):
    _stub_kb(monkeypatch)

    assert client.post(_PATH, json={"kb_id": "kb1", "metadata": "author"}).status_code == 422
    assert client.post(_PATH, json={"kb_id": "kb1"}).status_code == 422


def test_rejects_users_without_access_and_writes_nothing(client, monkeypatch):
    updates = _stub_kb(monkeypatch, accessible=False)

    resp = client.post(_PATH, json={"kb_id": "kb1", "metadata": _SETTINGS})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retcode"] == RetCode.AUTHENTICATION_ERROR, body
    assert updates == [], updates


def test_reports_missing_dataset(client, monkeypatch):
    updates = _stub_kb(monkeypatch, exists=False)

    resp = client.post(_PATH, json={"kb_id": "kb1", "metadata": _SETTINGS})

    body = resp.json()
    assert body["retcode"] == RetCode.DATA_ERROR, body
    assert updates == [], updates
