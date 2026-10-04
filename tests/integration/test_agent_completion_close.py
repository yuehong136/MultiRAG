"""Actual HTTP/socket closure with controlled ASGI send or Canvas EOF waits.

The send wait holds a real response iterator at its yield; it does not claim
kernel buffer saturation. Auth, Canvas, PostgreSQL and Redis remain real."""

from typing import Any

import pytest
import uvicorn  # noqa: F401 -- initialize before legacy nest_asyncio patches

from tests.support.agent_completion_close import close_case
from tests.support.agent_update_release import release_api as release_api
from tests.support.task_cancellation import cancel_api as cancel_api


@pytest.mark.parametrize("surface", ["rest", "beta", "openai"])
@pytest.mark.parametrize("mode", ["first", "continued", "published", "draft"])
@pytest.mark.parametrize("window,winner", [("send", "cancel"), ("canvas", "cancel"), ("send", "finish")])
def test_actual_socket_close(cancel_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, surface: str, mode: str, window: str, winner: str) -> None:
    close_case(cancel_api, monkeypatch, surface=surface, mode=mode, window=window, winner=winner)


@pytest.mark.parametrize("window,winner", [("send", "cancel"), ("canvas", "cancel"), ("send", "finish")])
def test_debug_socket_close(cancel_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, window: str, winner: str) -> None:
    close_case(cancel_api, monkeypatch, surface="debug", mode="first", window=window, winner=winner)
