from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

import api.apps.services.dataset_api_service as dataset_service
from api.apps.restful_apis.dataset_api import CreateDatasetRequest
from api.utils.validation_utils import CreateDatasetReq

PIPELINE_ID = "a" * 32


def test_create_dataset_req_allows_chunk_method_with_parse_type_one():
    req = CreateDatasetReq(name="ds", chunk_method="naive", parse_type=1)

    assert req.chunk_method == "naive"
    assert req.parse_type == 1
    assert req.pipeline_id is None


def test_create_dataset_req_allows_pipeline_with_parse_type_two():
    req = CreateDatasetReq(name="ds", parse_type=2, pipeline_id=PIPELINE_ID)

    assert req.chunk_method is None
    assert req.parse_type == 2
    assert req.pipeline_id == PIPELINE_ID


def test_create_dataset_req_rejects_chunk_method_with_pipeline_parse_type():
    with pytest.raises(ValidationError) as exc_info:
        CreateDatasetReq(name="ds", chunk_method="naive", parse_type=2, pipeline_id=PIPELINE_ID)

    assert "disallowed fields present" in str(exc_info.value)


def test_rest_create_dataset_request_accepts_pipeline_fields():
    req = CreateDatasetRequest(name="ds", parse_type=2, pipeline_id=PIPELINE_ID)

    payload = req.model_dump()
    assert payload["chunk_method"] is None
    assert payload["parse_type"] == 2
    assert payload["pipeline_id"] == PIPELINE_ID


def test_rest_create_dataset_request_rejects_invalid_mixed_parser_and_pipeline():
    with pytest.raises(ValidationError):
        CreateDatasetRequest(name="ds", chunk_method="naive", parse_type=2, pipeline_id=PIPELINE_ID)


def test_create_dataset_service_drops_parse_type_before_save(monkeypatch):
    saved = {}

    monkeypatch.setattr(dataset_service.KnowledgebaseService, "get_or_none", lambda db, **kwargs: None)
    monkeypatch.setattr(dataset_service, "get_parser_config", lambda parser_id, parser_config: {})
    monkeypatch.setattr(dataset_service, "ensure_tenant_model_id_for_params", lambda db, tenant_id, payload: payload)
    monkeypatch.setattr(dataset_service, "remap_dictionary_keys", lambda payload: payload)

    def fake_create_with_name(db, *, name, tenant_id, parser_id=None, embd_id=None, parser_config=None, **kwargs):
        return True, {
            "id": "kb1",
            "name": name,
            "tenant_id": tenant_id,
            "parser_id": parser_id,
            "embd_id": embd_id or "bge@builtin",
            **kwargs,
        }

    def fake_save(db, **payload):
        saved.update(payload)
        return True

    monkeypatch.setattr(dataset_service.KnowledgebaseService, "create_with_name", fake_create_with_name)
    monkeypatch.setattr(dataset_service.KnowledgebaseService, "save", fake_save)
    monkeypatch.setattr(dataset_service.KnowledgebaseService, "get_by_id", lambda db, kb_id: SimpleNamespace(to_dict=lambda: saved))

    ok, data = dataset_service.create_dataset(
        Session(),
        "tenant1",
        {"name": "ds", "parse_type": 2, "pipeline_id": PIPELINE_ID},
    )

    assert ok is True, data
    assert saved["pipeline_id"] == PIPELINE_ID
    assert "parse_type" not in saved
