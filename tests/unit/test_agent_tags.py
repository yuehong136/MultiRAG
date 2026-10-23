"""Tag normalization and registered REST contract boundaries."""

from typing import Any

import pytest

from api.db.services.canvas_service import UserCanvasService


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (" ml, ml-ops, ML, ,中文 ", "ml,ml-ops,中文"),
        ([" a,b ", "A B", "", "a"], "a b,a"),
        (["x" * 65, "X" * 64, "Y"], "x" * 64 + ",Y"),
        (["x" * 64] + [str(i) * 64 for i in range(10)], ",".join(["x" * 64] + [str(i) * 64 for i in range(6)])),
        ([], ""),
        ("", ""),
        (["%", "_", "a/b"], "%,_,a/b"),
    ],
)
def test_normalize_tags(tags: str | list[str], expected: str) -> None:
    assert UserCanvasService.normalize_tags(tags) == expected
    assert len(expected) <= 512


@pytest.mark.parametrize("body", [{}, {"tags": None}, {"tags": 1}, {"tags": [1]}, {"tags": {}}, {"tags": [], "tenant_id": "other"}])
def test_invalid_body_cannot_reach_write(client: Any, monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("Invalid tags reached SQL")

    monkeypatch.setattr(UserCanvasService, "update_tags", forbidden)
    response = client.put("/api/v1/agents/agent-id/tags", json=body)
    assert response.status_code == 422


@pytest.mark.parametrize("changed", [True, False])
def test_update_tags_reports_zero_rows_as_failure(client: Any, monkeypatch: pytest.MonkeyPatch, changed: bool) -> None:
    calls: list[tuple[str, str, Any]] = []

    async def update(db: Any, canvas_id: str, user_id: str, tags: str | list[str]) -> bool:
        calls.append((canvas_id, user_id, tags))
        return changed

    monkeypatch.setattr(UserCanvasService, "update_tags", update)
    response = client.put("/api/v1/agents/agent-id/tags", json={"tags": ["ML"]})
    assert response.status_code == 200
    assert (response.json()["retcode"] == 0) is changed
    assert response.json()["data"] is changed
    assert calls == [("agent-id", "user-unit", ["ML"])]
