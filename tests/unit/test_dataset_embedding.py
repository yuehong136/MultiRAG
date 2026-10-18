"""Strict comparison, read-only scope and embedding check HTTP contracts."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services import dataset_embedding_service as svc
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.constants import RetCode


@pytest.fixture
def comparison(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    chunk = {"kb_id": "kb", "doc_id": "doc", "docnm_kwd": "title", "content_with_weight": "body", "q_2_vec": [1, 0]}
    store = SimpleNamespace(
        index_exist=Mock(return_value=True), search=Mock(return_value="rows"), get_total=Mock(return_value=1), get_doc_ids=Mock(return_value=["chunk"]), get=Mock(return_value=chunk)
    )
    model = SimpleNamespace(encode=Mock(return_value=(np.array([[1, 0], [1, 0]]), 0)))
    monkeypatch.setattr(svc.settings, "docStoreConn", store)
    return store, model


def test_comparison_uses_dataset_owner_and_collection_and_does_not_write(comparison: tuple[Any, Any]) -> None:
    store, model = comparison
    success, data, code = svc._compare("owner", "kb", "name", model, "candidate", 5)
    assert success and code == RetCode.SUCCESS and data["summary"]["avg_cos_sim"] == 1
    store.index_exist.assert_called_once_with("multirag_owner_name", "kb")
    assert store.search.call_args.kwargs["dataset_ids"] == ["kb"]
    assert store.search.call_args.kwargs["index_names"] == ["multirag_owner_name"]
    store.get.assert_called_once_with("chunk", "multirag_owner_name", ["kb"])


@pytest.mark.parametrize("value", [[], [0, 0], [float("nan"), 1], [float("inf"), 1], [[1, 0]], [1e308, 1e308]])
def test_rejects_invalid_vectors(value: Any) -> None:
    with pytest.raises(ValueError):
        svc._vector(value)


@pytest.mark.parametrize("case", ["dimension", "count", "field", "scope"])
def test_malformed_sample_never_reports_compatibility(comparison: tuple[Any, Any], case: str) -> None:
    store, model = comparison
    if case == "dimension":
        model.encode.return_value = ([[1, 0, 0], [1, 0, 0]], 0)
    elif case == "count":
        model.encode.return_value = ([[1, 0]], 0)
    elif case == "field":
        store.get.return_value["q_3_vec"] = [1, 0, 0]
    else:
        store.get.return_value["kb_id"] = "foreign"
    with pytest.raises(ValueError):
        svc._compare("owner", "kb", "name", model, "candidate", 5)


def test_threshold_is_compared_before_rounding(comparison: tuple[Any, Any]) -> None:
    _, model = comparison
    similarity = 0.8999996
    model.encode.return_value = (np.array([[similarity, np.sqrt(1 - similarity**2)]] * 2), 0)
    success, data, code = svc._compare("owner", "kb", "name", model, "candidate", 1)
    assert not success and code == RetCode.NOT_EFFECTIVE
    assert data["summary"]["avg_cos_sim"] == 0.9


@pytest.mark.parametrize("empty", ["missing_index", "no_rows", "no_text"])
def test_empty_samples_do_not_claim_compatibility(comparison: tuple[Any, Any], empty: str) -> None:
    store, model = comparison
    if empty == "missing_index":
        store.index_exist.return_value = False
    elif empty == "no_rows":
        store.get_total.return_value = 0
    else:
        store.get.return_value["content_with_weight"] = ""
    success, _, code = svc._compare("owner", "kb", "name", model, "candidate", 5)
    assert not success and code == RetCode.DATA_ERROR
    model.encode.assert_not_called()


async def test_denial_precedes_model_and_storage(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KnowledgebaseService, "accessible_async", AsyncMock(return_value=False))
    build = Mock(side_effect=AssertionError("must not resolve a model"))
    monkeypatch.setattr(svc, "get_model_config_by_type_and_name", build)
    assert await svc.check_embedding(async_db, "other", "kb", "model") == (False, "No authorization.", RetCode.AUTHENTICATION_ERROR)
    build.assert_not_called()


@pytest.mark.parametrize("count", [0, -1, True, "5", 101, 1.5])
def test_route_validates_sample_budget(client: TestClient, count: Any) -> None:
    assert client.post("/api/v1/datasets/kb/embedding/check", json={"embd_id": "model", "check_num": count}).status_code == 422


def test_route_preserves_incompatible_result(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "check_embedding", AsyncMock(return_value=(False, {"summary": {"avg_cos_sim": 0.2}}, RetCode.NOT_EFFECTIVE)))
    response = client.post("/api/v1/datasets/kb/embedding/check", json={"embd_id": "model"})
    assert response.status_code == 200
    assert response.json()["code"] == RetCode.NOT_EFFECTIVE
    assert response.json()["data"]["summary"]["avg_cos_sim"] == 0.2


def test_retired_embedding_execution_route_stays_absent(client: TestClient) -> None:
    assert client.post("/api/v1/datasets/kb/embedding", json={}).status_code == 404


def test_cancelled_title_mix_does_not_discard_valid_content(comparison: tuple[Any, Any]) -> None:
    _, model = comparison
    model.encode.return_value = (np.array([[-9, 0], [1, 0]]), 0)
    success, data, _ = svc._compare("owner", "kb", "name", model, "candidate", 1)
    assert success and data["summary"]["avg_cos_sim"] == 1
