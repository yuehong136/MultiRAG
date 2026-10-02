"""PATCH-only Web numeric and SDK typed error bridge, including dependencies."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException

from api.utils.document_update_contract import DocumentUpdateError

logger = logging.getLogger(__name__)


def _contains_null(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_null(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_null(item) for item in value)
    return value is None


def update_error_response(error: DocumentUpdateError, request_id: str, challenge: str | None = None) -> Response:
    details = {"outcome": error.outcome}
    headers = {"X-Request-ID": request_id}
    if challenge:
        headers["WWW-Authenticate"] = challenge
    return JSONResponse(
        status_code=error.status,
        headers=headers,
        content={
            "code": error.code,
            "detail": str(error),
            "details": details,
            "request_id": request_id,
            "retcode": error.numeric_code,
            "retmsg": str(error),
            "message": str(error),
            "data": details,
        },
    )


class DocumentUpdateRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def update(request: Request) -> Response:
            request_id = uuid4().hex
            request.scope["document_update_request_id"] = request_id
            try:
                response = await handler(request)
            except RequestValidationError as error:
                errors = error.errors()
                semantic = bool(errors) and all(
                    tuple(item["loc"][:2]) == ("body", "parser_config")
                    and item["type"] not in {"extra_forbidden", "missing", "json_invalid", "model_type"}
                    and not item["type"].endswith("_type")
                    and not _contains_null(item.get("input"))
                    for item in errors
                )
                return update_error_response(
                    DocumentUpdateError(
                        "Invalid document update request.",
                        status=400 if semantic else 422,
                        numeric_code=102 if semantic else 101,
                        code="DOCUMENT_UPDATE_INVALID" if semantic else "DOCUMENT_UPDATE_VALIDATION",
                    ),
                    request_id,
                )
            except HTTPException as error:
                if error.status_code in {401, 403}:
                    challenge = next((v for k, v in (error.headers or {}).items() if k.lower() == "www-authenticate"), None)
                    return update_error_response(
                        DocumentUpdateError("Unauthorized", status=error.status_code, numeric_code=401 if error.status_code == 401 else 109, code="DOCUMENT_UPDATE_UNAUTHORIZED"), request_id, challenge
                    )
                return update_error_response(DocumentUpdateError("Document resource is unavailable.", status=404, code="DOCUMENT_UPDATE_UNAVAILABLE"), request_id)
            except DocumentUpdateError as error:
                return update_error_response(error, request_id)
            except Exception:
                logger.error("Unexpected document update failure.")
                return update_error_response(
                    DocumentUpdateError("Document update outcome could not be confirmed.", status=500, numeric_code=500, code="DOCUMENT_UPDATE_OUTCOME_UNKNOWN", outcome="unknown"), request_id
                )
            response.headers["X-Request-ID"] = request_id
            return response

        return update
