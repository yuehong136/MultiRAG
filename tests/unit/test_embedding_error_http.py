"""Active dataset routes retain a safe failure envelope and HTTP error status."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from api.apps.services import dataset_search_service
from common.constants import RetCode
from common.exceptions import ModelException


@pytest.mark.parametrize("collection", [False, True])
@pytest.mark.parametrize("retryable", [False, True])
def test_dataset_model_failure_preserves_rest_envelope(client: TestClient, monkeypatch: pytest.MonkeyPatch, collection: bool, retryable: bool) -> None:
    failure = ModelException(
        "Embedding provider request failed (HTTP 429)" if retryable else "Embedding provider request failed (HTTP 401)", status_code=429 if retryable else 401, retryable=retryable
    )
    call = AsyncMock(side_effect=failure)
    monkeypatch.setattr(dataset_search_service, "search_datasets" if collection else "search_dataset", call)
    response = client.post("/api/v1/datasets/search" if collection else "/api/v1/datasets/a/search", json={"question": "query", "dataset_ids": ["a"]})
    assert response.status_code == (503 if retryable else 502)
    assert response.json() == {"code": RetCode.EXCEPTION_ERROR, "message": str(failure), "data": {"retryable": retryable}}
    assert call.await_count == 1


def test_legacy_error_wrapper_uses_same_safe_boundary() -> None:
    from api.utils.api_utils import model_error_response, server_error_response

    error = ModelException("Embedding provider request failed", retryable=True)
    assert server_error_response(error).body == model_error_response(error).body
    assert server_error_response(error).status_code == 503
