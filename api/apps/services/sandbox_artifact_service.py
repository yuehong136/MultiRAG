"""Authorized downloads for files produced by CodeExec sandboxes."""

from __future__ import annotations

import re

from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import API4Conversation
from api.db.services.canvas_service import UserCanvasService
from api.utils.api_utils import get_data_error_result
from api.utils.web_utils import apply_safe_file_response_headers, should_force_attachment
from common import settings
from common.constants import SANDBOX_ARTIFACT_BUCKET
from common.misc_utils import thread_pool_exec
from core.utils.sandbox_artifact_registry import ArtifactBinding, get_artifact_binding

ARTIFACT_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
}

_ARTIFACT_FILENAME = re.compile(r"[0-9a-f]{32}\.([a-z0-9]+)\Z")


def _artifact_extension(filename: str) -> str:
    """Only accept the UUID-derived object names emitted by CodeExec."""
    match = _ARTIFACT_FILENAME.fullmatch(filename)
    if match is None:
        raise ValueError("Invalid filename.")
    ext = f".{match.group(1)}"
    if ext not in ARTIFACT_CONTENT_TYPES:
        raise ValueError("Invalid file type.")
    return ext


async def _artifact_accessible(db: AsyncSession, filename: str, user_id: str, run_id: str | None, session_id: str | None) -> bool:
    """Require a server-created filename binding to this user and execution."""
    if not run_id:
        return False
    binding: ArtifactBinding | None = await thread_pool_exec(get_artifact_binding, filename)
    if binding is None or binding.owner_id != user_id or binding.run_id != run_id or binding.session_id != session_id:
        return False
    if session_id is None:
        # Debug runs have no saved conversation; the exact object/run/owner
        # binding is their authorization record until the storage TTL expires.
        return True
    dialog_id = await db.scalar(select(API4Conversation.dialog_id).where(API4Conversation.id == session_id, API4Conversation.source == "agent"))
    if not dialog_id:
        return False
    return bool(await db.run_sync(lambda session: UserCanvasService.accessible(session, dialog_id, user_id)))  # TODO(async-phase4)


async def download_artifact(filename: str, user_id: str, db: AsyncSession, *, run_id: str | None = None, session_id: str | None = None) -> Response:
    """Serve an authorized artifact with a content type determined by its suffix."""
    try:
        ext = _artifact_extension(filename)
    except ValueError as exc:
        return get_data_error_result(retmsg=str(exc))

    if not await _artifact_accessible(db, filename, user_id, run_id, session_id):
        return get_data_error_result(retmsg="Artifact not found.")

    data = await thread_pool_exec(settings.STORAGE_IMPL.get, SANDBOX_ARTIFACT_BUCKET, filename)
    if not data:
        return get_data_error_result(retmsg="Artifact not found.")

    content_type = ARTIFACT_CONTENT_TYPES[ext]
    response = Response(content=data, media_type=content_type)
    apply_safe_file_response_headers(response, content_type, ext)
    disposition = "attachment" if should_force_attachment(ext, content_type) else "inline"
    response.headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response
