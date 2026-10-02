"""HTTP adaptation scoped to the three authorized image read endpoints."""

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException

from api.db.services.document_image_service import ImageStorageFailure, ImageUnavailable, InvalidImageBytes, InvalidImageInput

IMAGE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
logger = logging.getLogger(__name__)


def thumbnail_url(dataset_id: str, thumbnail: str | None) -> str | None:
    if not thumbnail or thumbnail.startswith("data:image/"):
        return thumbnail
    return f"/api/v1/documents/images/{dataset_id}-{quote(thumbnail, safe='')}"


def _error(status: int, code: int, message: str, challenge: str | None = None) -> Response:
    headers = {**IMAGE_HEADERS}
    if challenge:
        headers["WWW-Authenticate"] = challenge
    return JSONResponse(status_code=status, content={"code": code, "message": message, "data": None}, headers=headers)


class ImageReadRoute(APIRoute):
    """Cover endpoint, dependency and validation errors without global changes."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def image_read(request: Request) -> Response:
            try:
                response = await handler(request)
            except RequestValidationError:
                return _error(422, 101, "Invalid image request.")
            except InvalidImageInput:
                return _error(400, 101, "Invalid image request.")
            except ImageUnavailable:
                return _error(404, 102, "Image is unavailable.")
            except InvalidImageBytes:
                return _error(415, 102, "Image data is invalid.")
            except ImageStorageFailure:
                return _error(500, 500, "Image could not be read.")
            except HTTPException as error:
                if error.status_code in {401, 403}:
                    challenge = next((value for key, value in (error.headers or {}).items() if key.lower() == "www-authenticate"), None)
                    return _error(error.status_code, 401 if error.status_code == 401 else 109, "Unauthorized", challenge)
                return _error(500, 500, "Image could not be read.")
            except Exception:
                logger.error("Unexpected document image read failure.")
                return _error(500, 500, "Image could not be read.")
            response.headers.update(IMAGE_HEADERS)
            return response

        return image_read
