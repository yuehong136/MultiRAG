"""Safe binary responses shared by REST and the generated-file compatibility route."""

import logging
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from api.db.services.document_file_service import DocumentDownloadDenied, DocumentFile, DocumentFileStorageError, DocumentFileUnavailable
from api.utils.web_utils import CONTENT_TYPE_MAP, apply_safe_file_response_headers

logger = logging.getLogger(__name__)


class DocumentFileRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def read_file(request: Request) -> Response:
            try:
                response = await handler(request)
            except DocumentFileUnavailable:
                response = JSONResponse(status_code=404, content={"code": 102, "message": "Document not found!", "data": None})
            except DocumentDownloadDenied:
                response = JSONResponse(status_code=403, content={"code": 109, "message": "No download permission. Please contact your administrator.", "data": None})
            except DocumentFileStorageError:
                logger.error("Document storage read failed.")
                response = JSONResponse(status_code=500, content={"code": 500, "message": "Document could not be read.", "data": None})
            response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
            return response

        return read_file


def document_file_response(file: DocumentFile, *, download: bool = False) -> Response:
    ext = PurePosixPath(file.name.lower()).suffix.removeprefix(".")
    fallback = "image" if file.visual else "application"
    content_type = CONTENT_TYPE_MAP.get(ext, f"{fallback}/{ext}") if ext else "application/octet-stream"
    response = Response(content=file.data, media_type=content_type)
    apply_safe_file_response_headers(response, content_type, ext)
    if download or response.headers.get("Content-Disposition") == "attachment":
        response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(file.name, safe='')}"
    return response


def generated_attachment_response(data: bytes, ext: str) -> Response:
    # A MIME hint must never introduce an arbitrary response header value.
    content_type = CONTENT_TYPE_MAP.get(ext.lower(), "application/octet-stream")
    response = Response(content=data, media_type=content_type)
    apply_safe_file_response_headers(response, content_type, ext)
    return response
