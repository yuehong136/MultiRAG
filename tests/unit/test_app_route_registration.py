"""Route module identity is independent of checkout ancestor directory names."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import api.apps as apps


def test_api_named_checkout_keeps_canonical_module_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "api" / "api"
    pages = package / "apps" / "sdk"
    pages.mkdir(parents=True)
    source = pages / "registration_probe.py"
    source.write_text('from fastapi import APIRouter\nrouter = APIRouter()\n@router.get("/probe")\ndef probe():\n    return {"ok": True}\n')
    include_router = Mock()
    monkeypatch.setattr(apps, "__file__", str(package / "apps" / "__init__.py"))
    monkeypatch.setattr(apps, "app", SimpleNamespace(include_router=include_router))
    module_name = "api.apps.sdk.registration_probe"
    monkeypatch.setitem(sys.modules, module_name, None)
    apps.register_page(source)
    module = sys.modules[module_name]
    assert module is not None and module.probe() == {"ok": True}
    include_router.assert_called_once_with(module.router, prefix="/api/v1", tags=["registration_probe"])


@pytest.mark.parametrize(
    ("method", "path", "module"),
    [
        ("POST", "/api/v1/chats/{chat_id}/completions", "bot"),
        ("POST", "/api/v1/searchbots/retrieval_test", "bot"),
        ("POST", "/api/v1/dify/retrieval", "dify_retrieval"),
        ("GET", "/api/v1/dify/retrieval/health", "dify_retrieval"),
        ("POST", "/api/v1/retrieval", "chunk"),
        ("POST", "/api/v1/datasets/{dataset_id}/chunks", "chunk"),
        ("DELETE", "/api/v1/datasets/{dataset_id}/chunks", "chunk"),
        ("GET", "/api/v1/datasets/{dataset_id}/documents/{document_id}", "document_download"),
        ("GET", "/api/v1/documents/{document_id}", "document_download"),
    ],
)
def test_migrated_routes_have_one_rest_owner(client: object, method: str, path: str, module: str) -> None:
    from fastapi.routing import iter_route_contexts

    matches = [ctx for ctx in iter_route_contexts(client.app.routes) if ctx.path == path and method in (ctx.original_route.methods or [])]
    assert len(matches) == 1
    assert matches[0].original_route.endpoint.__module__ == f"api.apps.restful_apis.{module}"
    assert all(
        ctx.original_route.endpoint.__module__ not in {"api.apps.sdk.session", "api.apps.sdk.doc", "api.apps.sdk.dify_retrieval"}
        for ctx in iter_route_contexts(client.app.routes)
        if hasattr(ctx.original_route, "endpoint")
    )
