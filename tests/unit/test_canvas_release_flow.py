import json
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from api.apps import canvas_app as canvas_app_module
from api.apps.sdk import session as session_module
from api.db import CanvasCategory
from api.db.db_models import UserCanvas
from api.db.services import canvas_service as canvas_service_module


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


def test_create_agent_session_uses_release_dsl(monkeypatch) -> None:
    fake_db = Session()
    captured: dict[str, object] = {}
    fake_canvas = SimpleNamespace(id="agent-1", dsl={"draft": True})

    monkeypatch.setattr(
        session_module.UserCanvasService,
        "query",
        lambda db, **kwargs: [object()],
    )

    def fake_get_agent_dsl_with_release(db, agent_id, release_mode=False, tenant_id=None):
        captured["helper_args"] = {
            "db": db,
            "agent_id": agent_id,
            "release_mode": release_mode,
            "tenant_id": tenant_id,
        }
        return fake_canvas, json.dumps({"published": True}, ensure_ascii=False)

    monkeypatch.setattr(
        session_module.UserCanvasService,
        "get_agent_dsl_with_release",
        fake_get_agent_dsl_with_release,
    )

    class FakeCanvas:
        def __init__(self, dsl, tenant_id, agent_id, canvas_id=None):
            captured["canvas_init"] = {
                "dsl": dsl,
                "tenant_id": tenant_id,
                "agent_id": agent_id,
                "canvas_id": canvas_id,
            }

        def reset(self):
            captured["reset_called"] = True

        def get_prologue(self):
            return "published-prologue"

        def __str__(self):
            return json.dumps({"normalized": True}, ensure_ascii=False)

    monkeypatch.setattr(session_module, "Canvas", FakeCanvas)
    monkeypatch.setattr(
        session_module.API4ConversationService,
        "save",
        lambda db, **kwargs: captured.setdefault("saved_conv", kwargs),
    )

    response = session_module.create_agent_session(
        "agent-1",
        session_module.CreateAgentSessionRequest(user_id="user-1", release=True),
        db=fake_db,
        tenant_id="tenant-1",
    )
    payload = json.loads(response.body)

    assert captured["helper_args"]["release_mode"] is True
    assert captured["canvas_init"]["dsl"] == json.dumps({"published": True}, ensure_ascii=False)
    assert captured["saved_conv"]["message"] == [{"role": "assistant", "content": "published-prologue"}]
    assert payload["data"]["agent_id"] == "agent-1"
    fake_db.close()


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
