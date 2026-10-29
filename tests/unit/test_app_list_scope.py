"""Explicit owner filters never widen to the actor's default membership scope."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services.app_list_scope import resolve_list_owner_ids


@pytest.mark.parametrize(
    "requested, expected",
    [(None, ["owner-a", "owner-b"]), ([], []), (["owner-b", "owner-b"], ["owner-b"])],
)
async def test_owner_filter_preserves_omitted_empty_and_selected_scope(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch, requested: list[str] | None, expected: list[str]) -> None:
    monkeypatch.setattr(async_db, "scalars", AsyncMock(return_value=SimpleNamespace(all=lambda: ["owner-b", "owner-a"])))

    assert await resolve_list_owner_ids(async_db, "actor", requested) == expected


async def test_mixed_foreign_owner_filter_is_rejected(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(async_db, "scalars", AsyncMock(return_value=SimpleNamespace(all=lambda: ["owner-a"])))

    with pytest.raises(PermissionError, match="Only authorized owner_ids"):
        await resolve_list_owner_ids(async_db, "actor", ["owner-a", "foreign"])
