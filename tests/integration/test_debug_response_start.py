"""Real registered debug route, with a controlled pre-iteration ASGI send wait."""

from typing import Any

import pytest
import uvicorn  # noqa: F401 -- initialize before legacy nest_asyncio patches

from tests.support.agent_update_release import release_api as release_api
from tests.support.debug_response_start import debug_start_case
from tests.support.task_cancellation import cancel_api as cancel_api


@pytest.mark.parametrize("failure", ["send_error", "disconnect"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_registered_debug_response_start(cancel_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str, cancelled: bool) -> None:
    debug_start_case(cancel_api, monkeypatch, failure=failure, cancelled=cancelled)
