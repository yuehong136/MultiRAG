"""Canonical ingestion query and shared positive retrieval limits."""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.sdk.dify_retrieval import RetrievalSetting
from api.apps.sdk.doc import RetrievalTestRequest
from api.apps.sdk.session import SearchBotRetrievalTestRequest
from api.apps.services import dataset_api_service
from api.db.services.knowledgebase_service import KnowledgebaseService


@pytest.mark.parametrize("value", [0, -1, -100])
def test_all_retrieval_requests_reject_nonpositive_top_k(value: int) -> None:
    for model, payload in [
        (RetrievalSetting, {}),
        (RetrievalTestRequest, {"question": "test", "dataset_ids": ["kb"]}),
        (SearchBotRetrievalTestRequest, {"question": "test", "kb_id": "kb"}),
    ]:
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            model.model_validate({**payload, "top_k": value})
        assert model.model_validate({**payload, "top_k": 1}).top_k == 1
        assert model.model_validate(payload).top_k == 1024


def test_ingestion_gateway_forwards_file_filters(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    service = AsyncMock(return_value=(True, {"logs": [], "total": 0}))
    monkeypatch.setattr(dataset_api_service, "list_ingestion_logs", service)
    response = client.get(
        "/api/v1/datasets/kb/ingestions",
        params={"log_type": "file", "keywords": "报告", "operation_status": ["1", "3"], "types": ["pdf", "txt"], "suffix": ["pdf"]},
    )
    assert response.status_code == 200 and response.json()["code"] == 0
    assert service.call_args.args[7:] == (["1", "3"], None, None, "file", "报告", ["pdf", "txt"], ["pdf"])


async def test_ingestion_rejects_unknown_kind_after_permission_check(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    access = AsyncMock(return_value=False)
    monkeypatch.setattr(KnowledgebaseService, "accessible_async", access)
    assert await dataset_api_service.list_ingestion_logs(async_db, "user", "kb", log_type="other") == (False, "No authorization.")
    access.return_value = True
    result: tuple[bool, Any] = await dataset_api_service.list_ingestion_logs(async_db, "user", "kb", log_type="other")
    assert result == (False, 'Invalid "log_type", expected "dataset" or "file"')
