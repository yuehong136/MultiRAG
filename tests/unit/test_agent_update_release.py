import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from api.apps.restful_apis import agent_api
from api.db.db_models import UserCanvas


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), (None, None), ("true", True), (" TRUE ", True), ("1", True), ("false", False), ("0", False), ("", False)],
)
def test_update_release_normalizes_without_string_truthiness(value: Any, expected: bool | None) -> None:
    assert agent_api.UpdateAgentRequest(release=value).release is expected


@pytest.mark.parametrize("value", ["yes", "invalid", 1, 0, [], {}])
def test_update_release_rejects_ambiguous_values(value: Any) -> None:
    with pytest.raises(ValidationError):
        agent_api.UpdateAgentRequest(release=value)


@pytest.mark.parametrize(
    ("payload", "release", "snapshot"),
    [
        ({"dsl": {}}, False, True),
        ({"dsl": {}, "release": None}, False, True),
        ({"release": True}, True, True),
        ({"release": "false"}, False, True),
        ({"title": " renamed "}, True, False),
        ({"release": None}, True, False),
        ({}, True, False),
    ],
)
def test_update_preserves_metadata_and_saves_release_only(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], release: bool, snapshot: bool) -> None:
    canvas = UserCanvas(id="agent-1", user_id="owner", title="original", canvas_category="agent_canvas", dsl={}, release=True)
    calls: list[dict[str, Any]] = []
    with Session() as db:
        monkeypatch.setattr(db, "scalar", lambda stmt: canvas)
        monkeypatch.setattr(agent_api.UserCanvasVersionService, "save_or_replace_latest", lambda db, **kwargs: calls.append(kwargs) or (None, True))
        monkeypatch.setattr(agent_api.CanvasReplicaService, "replace_for_set", lambda **kwargs: True)
        result = agent_api.update_agent("agent-1", agent_api.UpdateAgentRequest(**payload), db, SimpleNamespace(id="owner", nickname="owner"))
    assert json.loads(result.body) == {"retcode": 0, "retmsg": "success", "data": True}
    assert canvas.release is release
    assert bool(calls) is snapshot
    if snapshot:
        assert calls[0]["release"] is release and calls[0]["commit"] is False
    if "title" in payload:
        assert canvas.title == "renamed"


def test_update_rejects_failed_version_result(monkeypatch: pytest.MonkeyPatch) -> None:
    canvas = UserCanvas(id="agent-1", user_id="owner", title="original", canvas_category="agent_canvas", dsl={}, release=False)
    replicas: list[dict[str, Any]] = []
    with Session() as db:
        monkeypatch.setattr(db, "scalar", lambda stmt: canvas)
        monkeypatch.setattr(agent_api.UserCanvasVersionService, "save_or_replace_latest", lambda db, **kwargs: (None, None))
        monkeypatch.setattr(agent_api.CanvasReplicaService, "replace_for_set", lambda **kwargs: replicas.append(kwargs))
        result = agent_api.update_agent("agent-1", agent_api.UpdateAgentRequest(release=True), db, SimpleNamespace(id="owner"))
    assert json.loads(result.body)["retcode"] != 0
    assert canvas.release is False and replicas == []
