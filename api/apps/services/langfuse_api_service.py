"""Langfuse credential operations for the canonical RESTful routes."""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict

from langfuse import Langfuse
from langfuse.api.core.api_error import ApiError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.services.langfuse_service import TenantLangfuseService
from api.utils.web_utils import validate_outbound_url
from common.app_config import get_app_config


class LangfuseKeys(TypedDict):
    tenant_id: str
    secret_key: str
    public_key: str
    host: str


class LangfuseCredentialError(ValueError):
    """Raised when Langfuse credentials are missing, unsafe, or invalid."""


class LangfuseRemoteError(RuntimeError):
    """Raised when Langfuse returns a typed API error during validation."""


def _credential_payload(tenant_id: str, secret_key: str, public_key: str, host: str) -> LangfuseKeys:
    if not all([secret_key, public_key, host]):
        raise LangfuseCredentialError("Missing required fields")
    return {
        "tenant_id": tenant_id,
        "secret_key": secret_key,
        "public_key": public_key,
        "host": host,
    }


def _validate_host(host: str) -> None:
    try:
        validate_outbound_url(host, get_app_config().observability.langfuse_allowed_hosts)
    except ValueError as exc:
        raise LangfuseCredentialError(f"Invalid Langfuse host: {exc}") from exc


def _client(credentials: dict[str, Any]) -> Any:
    return Langfuse(
        public_key=credentials["public_key"],
        secret_key=credentials["secret_key"],
        host=credentials["host"],
    )


def _validate_new_credentials(credentials: LangfuseKeys) -> None:
    _validate_host(credentials["host"])
    if not _client(credentials).auth_check():
        raise LangfuseCredentialError("Invalid Langfuse keys")


def _load_credentials_info(credentials: dict[str, Any]) -> dict[str, Any]:
    _validate_host(credentials["host"])
    langfuse = _client(credentials)
    try:
        if not langfuse.auth_check():
            raise LangfuseCredentialError("Invalid Langfuse keys loaded")
    except ApiError as exc:
        raise LangfuseRemoteError(f"Error from Langfuse: {exc}") from exc

    project = langfuse.api.projects.get().model_dump()["data"][0]
    result = dict(credentials)
    result["project_id"] = project["id"]
    result["project_name"] = project["name"]
    return result


def _save_credentials(db: Session, credentials: LangfuseKeys) -> None:
    tenant_id = credentials["tenant_id"]
    entry = TenantLangfuseService.filter_by_tenant(db, tenant_id=tenant_id)
    if entry:
        TenantLangfuseService.update_by_tenant(db, tenant_id=tenant_id, langfuse_keys=credentials)
    else:
        TenantLangfuseService.save(db, **credentials)


def _find_credentials(db: Session, tenant_id: str) -> dict[str, Any]:
    entry = TenantLangfuseService.filter_by_tenant_with_info(db, tenant_id=tenant_id)
    if not entry:
        raise LangfuseCredentialError("Have not record any Langfuse keys.")
    return entry


def _delete_credentials(db: Session, tenant_id: str) -> None:
    entry = TenantLangfuseService.filter_by_tenant(db, tenant_id=tenant_id)
    if not entry:
        raise LangfuseCredentialError("Have not record any Langfuse keys.")
    TenantLangfuseService.delete_model(db, entry)


async def set_credentials(
    db: AsyncSession,
    tenant_id: str,
    secret_key: str,
    public_key: str,
    host: str,
) -> LangfuseKeys:
    credentials = _credential_payload(tenant_id, secret_key, public_key, host)
    await asyncio.to_thread(_validate_new_credentials, credentials)
    await db.run_sync(lambda session: _save_credentials(session, credentials))  # TODO(async-phase4)
    return credentials


async def get_credentials(db: AsyncSession, tenant_id: str) -> dict[str, Any]:
    credentials = await db.run_sync(lambda session: _find_credentials(session, tenant_id))  # TODO(async-phase4)
    return await asyncio.to_thread(_load_credentials_info, credentials)


async def delete_credentials(db: AsyncSession, tenant_id: str) -> bool:
    await db.run_sync(lambda session: _delete_credentials(session, tenant_id))  # TODO(async-phase4)
    return True
