"""Stable FastAPI dependency providers for the Channel control plane.

REST modules are loaded under generated module names by ``api.apps``.  Keeping
dependency callables in a regular package module gives tests and future
embedders one canonical object to override.
"""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.channel_control.repository import SqlAlchemyChannelRepository
from api.channel_control.secret_store import SecretStore, get_channel_secret_store
from api.channel_control.service import ChannelControlService
from api.db.db_models import get_async_db


def get_channel_control_service(
    db: AsyncSession = Depends(get_async_db),
    secret_store: SecretStore = Depends(get_channel_secret_store),
) -> ChannelControlService:
    """Build the request-scoped MultiRAG Channel control service."""

    return ChannelControlService(SqlAlchemyChannelRepository(db), secret_store)


# The explicit alias documents the private route's narrower use without
# creating a second dependency object that tests or embedders must override.
get_runtime_control_service = get_channel_control_service
