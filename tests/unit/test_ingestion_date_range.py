"""Valid but reversed dates must not become a successful empty result."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services import dataset_api_service
from api.db.services.knowledgebase_service import KnowledgebaseService


@pytest.mark.parametrize(
    "start,end",
    [(datetime(2025, 2, 1), datetime(2025, 1, 1)), (datetime(2025, 1, 2, 1, tzinfo=UTC), datetime(2025, 1, 2))],
)
async def test_reversed_ingestion_dates_are_business_errors_before_log_query(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch, start: datetime, end: datetime) -> None:
    monkeypatch.setattr(KnowledgebaseService, "accessible_async", AsyncMock(return_value=True))
    query = AsyncMock()
    monkeypatch.setattr(async_db, "scalar", query)
    assert await dataset_api_service.list_ingestion_logs(async_db, "owner", "dataset", create_date_from=start, create_date_to=end) == (False, "create_date_from must not be later than create_date_to")
    query.assert_not_awaited()
