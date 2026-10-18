"""Read dataset documents and generated attachments as distinct resources."""

import asyncio
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Document, Knowledgebase, UserTenant
from api.db.services.file2document_service import File2DocumentService
from api.db.services.user_service import UserTenantService
from api.identity.principal import Principal
from common import settings


class DocumentFileUnavailable(ValueError):
    """Missing and inaccessible resource IDs have the same public result."""


class DocumentDownloadDenied(ValueError):
    """A readable dataset document requires a management role to download."""


class DocumentFileStorageError(ValueError):
    """The storage adapter failed or returned invalid bytes."""


@dataclass(frozen=True, slots=True)
class DocumentFile:
    data: bytes
    name: str
    visual: bool = False


def _read_object(namespace: str, key: str) -> bytes:
    try:
        storage = settings.STORAGE_IMPL
        reader = getattr(storage, "get_bytes", None) or storage.get
        data = reader(namespace, key)
    except Exception:
        raise DocumentFileStorageError() from None
    if data is None:
        raise DocumentFileUnavailable()
    if not isinstance(data, bytes):
        raise DocumentFileStorageError()
    return data


async def read_document_file(db: AsyncSession, principal: Principal, doc_id: str, *, download: bool = False) -> DocumentFile:
    row = (
        await db.execute(
            select(Document, Knowledgebase, UserTenant.role)
            .join(Knowledgebase, Knowledgebase.id == Document.kb_id)
            .join(UserTenant, UserTenant.tenant_id == Knowledgebase.tenant_id)
            .where(Document.id == doc_id, Document.status == "1", Knowledgebase.status == "1", UserTenant.user_id == principal.platform_user_id, UserTenant.status == "1")
            .limit(1)
        )
    ).first()
    if row is None or not UserTenantService.can_access_tenant_resources(row[2]):
        raise DocumentFileUnavailable()
    doc, _kb, role = row
    if download and not UserTenantService.can_download(role):
        raise DocumentDownloadDenied()
    namespace, key = await db.run_sync(lambda session: File2DocumentService.get_storage_address(session, doc_id=doc_id))  # TODO(async-phase4): legacy storage mapping uses only this request session.
    data = await asyncio.to_thread(_read_object, namespace, key)
    return DocumentFile(data=data, name=doc.name, visual=doc.type == "visual")


async def read_generated_attachment(principal: Principal, attachment_id: str) -> bytes:
    # These objects are created by DocGenerator in the authenticated user's
    # namespace. They are not Document rows or upload-info sidecar resources.
    if re.fullmatch(r"[0-9a-f]{32}", attachment_id) is None:
        raise DocumentFileUnavailable()
    return await asyncio.to_thread(_read_object, principal.platform_user_id, attachment_id)
