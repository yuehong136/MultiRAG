"""Authorized, registered dataset and owner-runtime raster image reads.

DB IO stays on the caller's AsyncSession; blocking object/index reads have no
session access. Authorization state is read fresh and no resource is written.
"""

import asyncio
import json
import re
import warnings
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import quote

from PIL import Image, ImageSequence
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Document, Knowledgebase, UserTenant
from api.db.services.file_service import FileService, RuntimeAttachmentError
from api.db.services.user_service import UserTenantService
from api.identity.principal import Principal
from common import settings
from common.doc_store.doc_store_base import OrderByExpr
from core.nlp.search import index_name_one


class ImageReadError(ValueError):
    """Safe image-read failure; transport details never form the wire message."""


class InvalidImageInput(ImageReadError):
    def __init__(self) -> None:
        super().__init__("Invalid image request.")


class ImageUnavailable(ImageReadError):
    def __init__(self) -> None:
        super().__init__("Image is unavailable.")


class InvalidImageBytes(ImageReadError):
    def __init__(self) -> None:
        super().__init__("Image data is invalid.")


class ImageStorageFailure(ImageReadError):
    def __init__(self) -> None:
        super().__init__("Image could not be read.")


@dataclass(frozen=True, slots=True)
class ImageBytes:
    data: bytes
    media_type: str


def _valid_id(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 1024 and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _split_image_id(image_id: str) -> tuple[str, str]:
    if not _valid_id(image_id):
        raise InvalidImageInput()
    namespace, separator, key = image_id.partition("-")
    if not separator or not _valid_id(namespace) or not _valid_id(key):
        raise InvalidImageInput()
    return namespace, key


def _raster(data: bytes) -> ImageBytes:
    # Use actual bytes rather than object extension or a caller-supplied MIME.
    formats = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp", "BMP": "image/bmp"}
    if not isinstance(data, bytes) or not data:
        raise InvalidImageBytes()
    magic = (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or data.startswith((b"GIF87a", b"GIF89a"))
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        or data.startswith(b"BM")
    )
    if not magic:
        raise InvalidImageBytes()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                media_type = formats.get(image.format or "")
                if media_type is None:
                    raise InvalidImageBytes()
                image.verify()
            with Image.open(BytesIO(data)) as image:
                for frame in ImageSequence.Iterator(image):
                    frame.load()
    except Exception:
        raise InvalidImageBytes() from None
    return ImageBytes(data=data, media_type=media_type)


def _read_object(namespace: str, key: str) -> bytes:
    try:
        storage = settings.STORAGE_IMPL
        read = getattr(storage, "get_bytes", None) or storage.get
        data = read(namespace, key)
    except Exception:
        raise ImageStorageFailure() from None
    if data is None:
        # Legacy adapters may swallow failures; unavailable does not claim 404.
        raise ImageUnavailable()
    if not isinstance(data, bytes):
        raise InvalidImageBytes()
    return data


async def _readable_dataset(db: AsyncSession, principal: Principal, kb_id: str) -> Knowledgebase | None:
    if not isinstance(principal, Principal):
        raise ImageUnavailable()
    row = (
        await db.execute(
            select(Knowledgebase, UserTenant.role)
            .join(UserTenant, UserTenant.tenant_id == Knowledgebase.tenant_id)
            .where(Knowledgebase.id == kb_id, Knowledgebase.status == "1", UserTenant.user_id == principal.platform_user_id, UserTenant.status == "1")
            .limit(1)
        )
    ).first()
    return row[0] if row and UserTenantService.can_access_tenant_resources(row[1]) else None


async def list_thumbnails(db: AsyncSession, principal: Principal, doc_ids: list[str]) -> dict[str, str | None]:
    if not isinstance(doc_ids, list) or not doc_ids or len(doc_ids) > 100 or any(not _valid_id(doc_id) for doc_id in doc_ids):
        raise InvalidImageInput()
    ids = list(dict.fromkeys(doc_ids))
    documents = (await db.execute(select(Document.id, Document.kb_id, Document.thumbnail).where(Document.id.in_(ids)))).all()
    rows = {row.id: row for row in documents}
    access: dict[str, bool] = {}
    result: dict[str, str | None] = {}
    for doc_id in ids:
        row = rows.get(doc_id)
        if row is None:
            continue
        if row.kb_id not in access:
            access[row.kb_id] = await _readable_dataset(db, principal, row.kb_id) is not None
        if not access[row.kb_id]:
            continue
        value = row.thumbnail
        result[doc_id] = value if not value or value.startswith("data:image/") else f"/api/v1/documents/images/{row.kb_id}-{quote(value, safe='')}"
    return result


def _registered_chunk_documents(tenant_id: str, kb_name: str, kb_id: str, image_id: str) -> list[str]:
    try:
        store = settings.docStoreConn
        result = store.search(["img_id", "doc_id", "kb_id"], [], {"img_id": image_id, "kb_id": kb_id}, [], OrderByExpr(), 0, 100, [index_name_one(tenant_id, kb_name)], [kb_id])
        if store.db_type() == "milvus":
            # Milvus search returns (rows, count). Its get_fields also adds a
            # distance list, contrary to its dict[str, dict] runtime annotation.
            rows = result[0] if isinstance(result, tuple) else result
        else:
            rows = list(store.get_fields(result, ["img_id", "doc_id", "kb_id"]).values())
        # Do not apply availability/mother filters: disabled/parent images are readable.
        return [
            row["doc_id"]
            for row in rows
            if isinstance(row, dict)
            and row.get("img_id") == image_id
            and (row.get("kb_id") == kb_id or (isinstance(row.get("kb_id"), list) and kb_id in row["kb_id"]))
            and isinstance(row.get("doc_id"), str)
        ]
    except Exception:
        raise ImageStorageFailure() from None


async def read_dataset_image(db: AsyncSession, principal: Principal, image_id: str, expected_dataset_id: str | None = None) -> ImageBytes:
    kb_id, key = _split_image_id(image_id)
    if expected_dataset_id is not None and expected_dataset_id != kb_id:
        raise ImageUnavailable()
    kb = await _readable_dataset(db, principal, kb_id)
    if kb is None:
        raise ImageUnavailable()
    document_id = await db.scalar(select(Document.id).where(Document.kb_id == kb_id, Document.thumbnail == key).limit(1))
    if document_id is None:
        doc_ids = await asyncio.to_thread(_registered_chunk_documents, kb.tenant_id, kb.name, kb_id, image_id)
        document_id = await db.scalar(select(Document.id).where(Document.kb_id == kb_id, Document.id.in_(doc_ids)).limit(1)) if doc_ids else None
    if document_id is None:
        raise ImageUnavailable()
    data = await asyncio.to_thread(_read_object, kb_id, key)
    return await asyncio.to_thread(_raster, data)


async def read_runtime_image(principal: Principal, file_id: str) -> ImageBytes:
    if not isinstance(principal, Principal):
        raise ImageUnavailable()
    if not isinstance(file_id, str) or re.fullmatch(r"[0-9a-f]{32}", file_id) is None:
        raise InvalidImageInput()
    owner = principal.platform_user_id
    namespace = f"{owner}-downloads"
    # A strict sidecar read detects adapter failures before legacy restoration.
    sidecar = await asyncio.to_thread(_read_object, namespace, FileService._runtime_descriptor_key(file_id))
    try:
        descriptors = await FileService.resolve_runtime_uploads(owner, [file_id])
        descriptor = descriptors[0]
        if json.loads(sidecar) != descriptor:
            raise ImageUnavailable()
    except (RuntimeAttachmentError, ValueError, TypeError, IndexError):
        raise ImageUnavailable() from None
    except Exception:
        raise ImageStorageFailure() from None
    data = await asyncio.to_thread(_read_object, namespace, file_id)
    if len(data) != descriptor["size"]:
        raise InvalidImageBytes()
    return await asyncio.to_thread(_raster, data)
