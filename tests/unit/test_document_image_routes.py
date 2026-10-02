"""Real route contracts; only the accepted image service boundary is faked."""

import asyncio
import inspect
import sys
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services.document_image_http import ImageReadRoute, thumbnail_url
from api.db.db_models import get_async_db
from api.db.services.document_image_service import ImageBytes, ImageStorageFailure, ImageUnavailable, InvalidImageBytes, InvalidImageInput
from api.identity.principal import Principal
from api.utils.api_utils import async_current_user

_READS = [
    ("/api/v1/thumbnails", "list_thumbnails", {"doc_ids": ["a", "b", "a"]}),
    ("/api/v1/documents/images/kb-key", "read_dataset_image", {}),
    ("/api/v1/documents/runtime/" + "a" * 32 + "/image", "read_runtime_image", {}),
]


def _module() -> Any:
    return sys.modules["api.apps.restful_apis.document"]


def _headers(response: Any) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-disposition" not in response.headers
    assert int(response.headers["content-length"]) == len(response.content)


def test_image_routes_have_real_async_dependencies_and_local_adapter(client: Any) -> None:
    contexts = {context.path: context.original_route for context in iter_route_contexts(client.app.routes) if isinstance(context.original_route, APIRoute)}
    for path in ["/api/v1/thumbnails", "/api/v1/documents/images/{image_id:path}", "/api/v1/documents/runtime/{file_id}/image"]:
        route = contexts[path]
        assert isinstance(route, ImageReadRoute)
        assert inspect.iscoroutinefunction(route.endpoint)
        dependencies = {dep.call for dep in route.dependant.dependencies}
        assert async_current_user in dependencies
        if "runtime" not in path:
            assert get_async_db in dependencies
        principal_dependency = next(dep for dep in route.dependant.dependencies if dep.call is async_current_user)
        assert get_async_db in {dep.call for dep in principal_dependency.dependencies}
    assert not isinstance(contexts["/api/v1/documents/ingest"], ImageReadRoute)


@pytest.mark.parametrize("path,function,params", _READS)
def test_image_success_uses_trusted_dependencies(client: Any, monkeypatch: pytest.MonkeyPatch, path: str, function: str, params: dict[str, Any]) -> None:
    calls: list[tuple[Any, ...]] = []

    async def read(*args: Any) -> Any:
        calls.append(args)
        return {"a": None, "b": "data:image/webp;base64,abc"} if function == "list_thumbnails" else ImageBytes(b"raw-raster-result", "image/webp")

    monkeypatch.setattr(_module(), function, read)
    response = client.get(path, params=params)
    assert response.status_code == 200
    _headers(response)
    if function == "list_thumbnails":
        assert response.json() == {"code": 0, "message": "success", "data": {"a": None, "b": "data:image/webp;base64,abc"}}
        assert calls[0][2] == ["a", "b", "a"]
    else:
        assert response.content == b"raw-raster-result"
        assert response.headers["content-type"] == "image/webp"
    if function != "read_runtime_image":
        assert isinstance(calls[0][0], AsyncSession)
        principal = calls[0][1]
    else:
        principal = calls[0][0]
    assert isinstance(principal, Principal) and principal.platform_user_id == "user-unit"


@pytest.mark.parametrize("path,function,params", _READS)
@pytest.mark.parametrize(
    "failure,status,code,message",
    [
        (InvalidImageInput, 400, 101, "Invalid image request."),
        (ImageUnavailable, 404, 102, "Image is unavailable."),
        (InvalidImageBytes, 415, 102, "Image data is invalid."),
        (ImageStorageFailure, 500, 500, "Image could not be read."),
        (ValueError, 500, 500, "Image could not be read."),
        (RuntimeError, 500, 500, "Image could not be read."),
    ],
)
def test_image_errors_are_safe_json(
    client: Any, monkeypatch: pytest.MonkeyPatch, path: str, function: str, params: dict[str, Any], failure: type[Exception], status: int, code: int, message: str
) -> None:
    async def read(*args: Any) -> Any:
        raise failure() if failure in {InvalidImageInput, ImageUnavailable, InvalidImageBytes, ImageStorageFailure} else failure("private-key/private-token")

    monkeypatch.setattr(_module(), function, read)
    response = client.get(path, params=params)
    assert response.status_code == status
    assert response.json() == {"code": code, "message": message, "data": None}
    assert response.headers["content-type"] == "application/json"
    _headers(response)


@pytest.mark.parametrize("status,code", [(401, 401), (403, 109)])
@pytest.mark.parametrize("path,function,params", _READS)
def test_image_dependency_rejections_have_headers_and_safe_challenge(client: Any, monkeypatch: pytest.MonkeyPatch, path: str, function: str, params: dict[str, Any], status: int, code: int) -> None:
    async def deny() -> Principal:
        raise HTTPException(status, "private principal detail", headers={"WWW-Authenticate": "Bearer", "X-Private": "secret"})

    async def forbid(*args: Any) -> Any:
        pytest.fail("rejected credential reached image service")

    client.app.dependency_overrides[async_current_user] = deny
    monkeypatch.setattr(_module(), function, forbid)
    response = client.get(path, params=params)
    assert response.status_code == status
    assert response.json() == {"code": code, "message": "Unauthorized", "data": None}
    _headers(response)
    assert response.headers["www-authenticate"] == "Bearer"
    assert "x-private" not in response.headers


def test_image_missing_query_validation_is_feature_local(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbid(*args: Any) -> Any:
        pytest.fail("invalid query reached service")

    monkeypatch.setattr(_module(), "list_thumbnails", forbid)
    response = client.get("/api/v1/thumbnails")
    assert response.status_code == 422
    assert response.json() == {"code": 101, "message": "Invalid image request.", "data": None}
    _headers(response)
    ordinary = client.post("/api/v1/documents/ingest", json={})
    assert ordinary.status_code == 422 and "cache-control" not in ordinary.headers


def test_image_openapi_removal_and_legacy_binary_retention(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/document/thumbnails" not in paths
    assert paths["/v1/document/image/{image_id}"]["get"]["deprecated"] is True
    assert "/api/v1/documents/images/{image_id}" in paths
    assert paths["/api/v1/thumbnails"]["get"]["parameters"][0]["schema"]["type"] == "array"
    from api.db.services.document_service import DocumentService

    def forbid(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("removed JSON route reached service")

    monkeypatch.setattr(DocumentService, "get_thumbnails", forbid)
    response = client.get("/v1/document/thumbnails", params={"doc_ids": "private"})
    assert response.status_code == 404 and response.json()["code"] == 404
    assert "cache-control" not in response.headers


@pytest.mark.parametrize("value", [None, "", "data:image/png;base64,abc", "data:image/jpeg;base64,abc", "data:image/webp;base64,abc"])
def test_inline_and_absent_thumbnail_are_preserved(value: str | None) -> None:
    assert thumbnail_url("kb", value) == value


def test_thumbnail_reserved_characters_are_encoded_once() -> None:
    assert thumbnail_url("kb", "a-b 空间%/image.png") == "/api/v1/documents/images/kb-a-b%20%E7%A9%BA%E9%97%B4%25%2Fimage.png"


async def test_route_does_not_swallow_cancellation(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.requests import Request

    route = next(context.original_route for context in iter_route_contexts(client.app.routes) if context.path == "/api/v1/thumbnails")

    async def cancelled(request: Request) -> Any:
        raise asyncio.CancelledError()

    monkeypatch.setattr(APIRoute, "get_route_handler", lambda self: cancelled)
    with pytest.raises(asyncio.CancelledError):
        await route.get_route_handler()(Request({"type": "http"}))
