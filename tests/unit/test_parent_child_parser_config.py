from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

import api.apps.services.dataset_api_service as dataset_service
from api.utils.api_utils import get_parser_config
from api.utils.validation_utils import CreateDatasetReq


def test_create_dataset_req_accepts_parent_child_config():
    req = CreateDatasetReq(
        name="ds",
        parser_config={
            "parent_child": {
                "use_parent_child": True,
                "children_delimiter": "@@",
            }
        },
    )

    dumped = req.model_dump()
    assert dumped["parser_config"]["parent_child"]["use_parent_child"] is True
    assert dumped["parser_config"]["parent_child"]["children_delimiter"] == "@@"


def test_create_dataset_req_rejects_empty_parent_child_delimiter():
    with pytest.raises(ValidationError) as exc_info:
        CreateDatasetReq(name="ds", parser_config={"parent_child": {"children_delimiter": ""}})

    assert "String should have at least 1 character" in str(exc_info.value)


def test_get_parser_config_flattens_enabled_parent_child_config():
    parser_config = get_parser_config(
        "naive",
        {
            "parent_child": {
                "use_parent_child": True,
                "children_delimiter": "\n\n",
            }
        },
    )

    assert parser_config["parent_child"]["use_parent_child"] is True
    assert parser_config["enable_children"] is True
    assert parser_config["children_delimiter"] == "\n\n"


def test_get_parser_config_clears_disabled_parent_child_delimiter():
    parser_config = get_parser_config(
        "naive",
        {
            "children_delimiter": "@@",
            "parent_child": {
                "use_parent_child": False,
                "children_delimiter": "\n\n",
            },
        },
    )

    assert parser_config["children_delimiter"] == ""
    assert parser_config["enable_children"] is False
    assert parser_config["parent_child"] == {}


def test_update_dataset_flattens_parent_child_config(monkeypatch):
    captured = {}
    kb_payload = {
        "id": "kb1",
        "tenant_id": "tenant1",
        "name": "old",
        "parser_id": "naive",
        "parser_config": {"delimiter": "\n", "children_delimiter": ""},
        "pipeline_id": "",
        "chunk_num": 0,
        "embd_id": "bge@builtin",
        "pagerank": 0,
    }
    kb = SimpleNamespace(**kb_payload)

    def fake_get_or_none(_db, **kwargs):
        if "id" in kwargs:
            return kb
        return None

    def fake_update_by_id(_db, _kb_id, payload):
        captured.update(payload)
        return True

    monkeypatch.setattr(dataset_service.KnowledgebaseService, "get_or_none", fake_get_or_none)
    monkeypatch.setattr(dataset_service.KnowledgebaseService, "update_by_id", fake_update_by_id)
    monkeypatch.setattr(
        dataset_service.KnowledgebaseService,
        "get_by_id",
        lambda _db, _kb_id: SimpleNamespace(to_dict=lambda: {**kb_payload, **captured}),
    )
    monkeypatch.setattr(dataset_service, "ensure_tenant_model_id_for_params", lambda _db, _tenant_id, payload: payload)
    monkeypatch.setattr(dataset_service, "remap_dictionary_keys", lambda payload: payload)

    ok, data = dataset_service.update_dataset(
        Session(),
        "tenant1",
        "kb1",
        {
            "parser_config": {
                "parent_child": {
                    "use_parent_child": True,
                    "children_delimiter": "@@",
                }
            }
        },
    )

    assert ok is True, data
    assert captured["parser_config"]["enable_children"] is True
    assert captured["parser_config"]["children_delimiter"] == "@@"


def test_update_dataset_clears_disabled_parent_child_config(monkeypatch):
    captured = {}
    kb_payload = {
        "id": "kb1",
        "tenant_id": "tenant1",
        "name": "old",
        "parser_id": "naive",
        "parser_config": {
            "children_delimiter": "@@",
            "enable_children": True,
            "parent_child": {
                "use_parent_child": True,
                "children_delimiter": "@@",
            },
        },
        "pipeline_id": "",
        "chunk_num": 0,
        "embd_id": "bge@builtin",
        "pagerank": 0,
    }
    kb = SimpleNamespace(**kb_payload)

    def fake_get_or_none(_db, **kwargs):
        if "id" in kwargs:
            return kb
        return None

    def fake_update_by_id(_db, _kb_id, payload):
        captured.update(payload)
        return True

    monkeypatch.setattr(dataset_service.KnowledgebaseService, "get_or_none", fake_get_or_none)
    monkeypatch.setattr(dataset_service.KnowledgebaseService, "update_by_id", fake_update_by_id)
    monkeypatch.setattr(
        dataset_service.KnowledgebaseService,
        "get_by_id",
        lambda _db, _kb_id: SimpleNamespace(to_dict=lambda: {**kb_payload, **captured}),
    )
    monkeypatch.setattr(dataset_service, "ensure_tenant_model_id_for_params", lambda _db, _tenant_id, payload: payload)
    monkeypatch.setattr(dataset_service, "remap_dictionary_keys", lambda payload: payload)

    ok, data = dataset_service.update_dataset(
        Session(),
        "tenant1",
        "kb1",
        {
            "parser_config": {
                "parent_child": {
                    "use_parent_child": False,
                    "children_delimiter": "@@",
                }
            }
        },
    )

    assert ok is True, data
    assert captured["parser_config"]["enable_children"] is False
    assert captured["parser_config"]["children_delimiter"] == ""
    assert captured["parser_config"]["parent_child"] == {}
