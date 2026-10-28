import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from agent import canvas as agent_canvas
from api.apps.restful_apis import agent_api as canvas_app_module
from api.db import CanvasCategory, TenantPermission
from api.db.db_models import UserCanvas
from api.db.services import canvas_service as canvas_service_module
from api.db.services.api_service import API4ConversationService
from api.identity.principal import Principal


def test_get_agent_dsl_with_release_prefers_latest_published_version(monkeypatch) -> None:
    fake_db = Session()
    fake_canvas = UserCanvas(id="agent-1", user_id="tenant-1", dsl={"draft": True})
    fake_release = SimpleNamespace(dsl={"published": True})

    monkeypatch.setattr(
        canvas_service_module.UserCanvasService,
        "get_by_id",
        lambda db, agent_id: fake_canvas,
    )
    monkeypatch.setattr(
        canvas_service_module.UserCanvasVersionService,
        "get_latest_released",
        lambda db, user_canvas_id: fake_release,
    )

    canvas, dsl = canvas_service_module.UserCanvasService.get_agent_dsl_with_release(
        fake_db,
        "agent-1",
        release_mode=True,
        tenant_id="tenant-1",
    )

    assert canvas is fake_canvas
    assert json.loads(dsl) == {"published": True}
    fake_db.close()


def test_get_agent_dsl_with_release_rejects_missing_published_version(monkeypatch) -> None:
    fake_db = Session()
    fake_canvas = UserCanvas(id="agent-1", user_id="tenant-1", dsl={"draft": True})

    monkeypatch.setattr(
        canvas_service_module.UserCanvasService,
        "get_by_id",
        lambda db, agent_id: fake_canvas,
    )
    monkeypatch.setattr(
        canvas_service_module.UserCanvasVersionService,
        "get_latest_released",
        lambda db, user_canvas_id: None,
    )

    with pytest.raises(PermissionError, match="No available published version"):
        canvas_service_module.UserCanvasService.get_agent_dsl_with_release(
            fake_db,
            "agent-1",
            release_mode=True,
            tenant_id="tenant-1",
        )

    fake_db.close()


def test_agent_accessible_allows_owner_and_team_members_only(monkeypatch) -> None:
    fake_db = Session()
    canvas = {"user_id": "owner-1", "permission": TenantPermission.ME.value}
    monkeypatch.setattr(
        canvas_service_module.UserCanvasService,
        "get_by_canvas_id",
        classmethod(lambda cls, db, canvas_id: (True, canvas)),
    )
    monkeypatch.setattr(fake_db, "scalar", lambda statement: "active-member")

    assert canvas_service_module.UserCanvasService.accessible(fake_db, "agent-1", "owner-1") is True
    assert canvas_service_module.UserCanvasService.accessible(fake_db, "agent-1", "member-1") is False

    canvas["permission"] = TenantPermission.TEAM.value
    assert canvas_service_module.UserCanvasService.accessible(fake_db, "agent-1", "member-1") is True
    fake_db.close()


def test_agent_session_date_filter_normalization_covers_full_end_day() -> None:
    assert API4ConversationService._normalize_query_date("2026-04-23").strftime("%Y-%m-%d %H:%M:%S") == "2026-04-23 00:00:00"
    assert API4ConversationService._normalize_query_date("2026-04-23", is_end=True).strftime("%Y-%m-%d %H:%M:%S") == "2026-04-23 23:59:59"


def test_create_agent_session_uses_release_dsl(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.restful_apis.agent"]
    captured: dict[str, Any] = {}
    run_context = object()

    async def prepare(db: AsyncSession, agent_id: str, caller_id: str, *, release_mode: bool, principal: Principal) -> SimpleNamespace:
        captured["release_mode"] = release_mode
        return SimpleNamespace(dsl=json.dumps({"published": True}), runtime_tenant_id=caller_id, run_context=run_context, version_title="published")

    async def save(db: AsyncSession, conversation: dict[str, Any], *, context: object, snapshot: dict[str, Any]) -> dict[str, Any]:
        captured["context"] = context
        captured["saved_conv"] = conversation
        assert snapshot == {"normalized": True}
        return conversation

    class FakeCanvas:
        def __init__(self, dsl: str, tenant_id: str, canvas_id: str | None = None, run_context: object | None = None) -> None:
            captured["canvas_init"] = {"dsl": dsl, "tenant_id": tenant_id, "canvas_id": canvas_id, "run_context": run_context}
            self.globals: dict[str, Any] = {}

        def reset(self) -> None:
            captured["reset_called"] = True

        def get_prologue(self) -> str:
            return "published-prologue"

        def __str__(self) -> str:
            return json.dumps({"normalized": True})

    monkeypatch.setattr(module, "prepare_agent_run", prepare)
    monkeypatch.setattr(module, "save_agent_session", save)
    monkeypatch.setattr(agent_canvas, "Canvas", FakeCanvas)
    response = client.post("/api/v1/agents/agent-1/sessions", json={"release": True, "user_id": "user-1"})
    payload = response.json()
    assert response.status_code == 200 and payload["retcode"] == 0, response.text
    assert captured["release_mode"] is True
    assert captured["canvas_init"]["dsl"] == json.dumps({"published": True})
    assert captured["canvas_init"]["run_context"] is run_context
    assert captured["saved_conv"]["message"] == [{"role": "assistant", "content": "published-prologue"}]
    assert captured["saved_conv"]["user_id"] == "user-unit"
    assert captured["saved_conv"]["exp_user_id"] == "user-1"
    assert captured["context"] is run_context
    assert payload["data"]["agent_id"] == "agent-1"


def test_canvas_get_returns_linked_datasets_for_dataflow(monkeypatch) -> None:
    fake_db = Session()
    fake_user = SimpleNamespace(id="tenant-1")

    monkeypatch.setattr(
        canvas_app_module.UserCanvasService,
        "accessible",
        lambda db, canvas_id, tenant_id: True,
    )
    monkeypatch.setattr(
        canvas_app_module.UserCanvasService,
        "get_by_canvas_id",
        lambda db, canvas_id: (
            True,
            {
                "id": canvas_id,
                "title": "pipeline-1",
                "dsl": {"nodes": []},
                "canvas_category": CanvasCategory.DataFlow,
            },
        ),
    )
    monkeypatch.setattr(canvas_app_module.CanvasReplicaService, "bootstrap", lambda **kwargs: None)
    monkeypatch.setattr(
        canvas_app_module.UserCanvasVersionService,
        "list_by_canvas_id",
        lambda db, canvas_id: [
            SimpleNamespace(release=False, update_time=100),
            SimpleNamespace(release=True, update_time=200),
            SimpleNamespace(release=True, update_time=150),
        ],
    )
    monkeypatch.setattr(
        canvas_app_module.KnowledgebaseService,
        "query",
        lambda db, **kwargs: [
            SimpleNamespace(id="kb-1", name="Knowledge Base 1", avatar="avatar-1"),
            SimpleNamespace(id="kb-2", name="Knowledge Base 2", avatar="avatar-2"),
        ],
    )

    response = canvas_app_module.get("canvas-1", db=fake_db, user=fake_user)
    payload = json.loads(response.body)

    assert payload["data"]["last_publish_time"] == 200
    assert payload["data"]["datasets"] == [
        {"id": "kb-1", "name": "Knowledge Base 1", "avatar": "avatar-1"},
        {"id": "kb-2", "name": "Knowledge Base 2", "avatar": "avatar-2"},
    ]
    fake_db.close()
