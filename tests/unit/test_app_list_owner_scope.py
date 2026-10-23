"""Owner filters are rejected before the app-list service is reached."""

import importlib
from typing import Any

import pytest

from api.apps.restful_apis import chat_api, search_api
from common.constants import RetCode


@pytest.mark.parametrize("module,service,path", [(chat_api, chat_api.DialogService, "/api/v1/chats"), (search_api, search_api.SearchService, "/api/v1/searches")])
def test_unauthorized_filter_does_not_read_app_rows(client: Any, monkeypatch: pytest.MonkeyPatch, module: Any, service: Any, path: str) -> None:
    async def reject(db: Any, actor: str, requested: list[str] | None) -> list[str]:
        assert actor == "user-unit" and requested == ["foreign"]
        raise PermissionError("Only authorized owner_ids can be queried.")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("An unauthorized request read business rows")

    registered = importlib.import_module(module.__name__.removesuffix("_api"))
    monkeypatch.setattr(registered, "resolve_list_owner_ids", reject)
    monkeypatch.setattr(service, "get_by_tenant_ids", forbidden)
    response = client.get(path, params={"owner_ids": "foreign"})
    assert response.status_code == 200
    assert response.json() == {"code": int(RetCode.OPERATING_ERROR), "message": "Only authorized owner_ids can be queried."}
