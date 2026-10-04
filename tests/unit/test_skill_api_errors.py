import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from api.apps.restful_apis.skill_api import SkillRoute
from api.skills.search import SkillSearchRuntime
from api.skills.search_types import SkillSearchError


def test_unsupported_search_backend_is_nonretryable_http_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOC_ENGINE", "elasticsearch")
    runtime = SkillSearchRuntime()
    with pytest.raises(SkillSearchError) as captured:
        runtime._require_store()
    assert captured.value.error_code == "SEARCH_BACKEND_UNSUPPORTED" and captured.value.retryable is False
    app = FastAPI()
    router = APIRouter(route_class=SkillRoute)

    @router.get("/search")
    async def unsupported() -> None:
        runtime._require_store()

    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/search")
    assert response.status_code == 503
    assert response.json()["data"]["error_code"] == "SEARCH_BACKEND_UNSUPPORTED"


def test_asset_protocol_is_explicit_and_alias_default_is_preserved() -> None:
    from api.apps.restful_apis.skill_api import router

    app = FastAPI()
    app.include_router(router)
    paths = app.openapi()["paths"]
    assert "/skill-assets/spaces" in paths and "/skills/spaces" in paths


def test_protocol_rejects_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.skills.protocol import selected_protocol

    monkeypatch.setenv("SKILLS_API_PROTOCOL", "guess-from-body")
    with pytest.raises(RuntimeError, match="SKILLS_API_PROTOCOL"):
        selected_protocol()
