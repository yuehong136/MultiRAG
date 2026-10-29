"""Memory API preflight and tenant model scoping, with explicit unit fakes."""

from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.apps.services import memory_api_service as service
from api.db.db_models import Memory
from api.db.joint_services import memory_message_service as mms
from api.db.joint_services import tenant_model_service as models
from api.db.services.memory_service import MemoryService
from common.exceptions import ArgumentException, NotFoundException


@pytest.fixture
def memories(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        Memory(id="own", tenant_id="user", permissions="me"),
        Memory(id="team", tenant_id="joined", permissions="team"),
        Memory(id="private", tenant_id="joined", permissions="me"),
        Memory(id="foreign", tenant_id="outsider", permissions="team"),
    ]
    monkeypatch.setattr(MemoryService, "accessible_tenant_ids", lambda db, user: {"user", "joined"})
    monkeypatch.setattr(MemoryService, "get_by_ids", lambda db, ids: [m for m in rows if m.id in ids])


@pytest.mark.parametrize("ids", [["private"], ["foreign"], ["missing"], ["own", "foreign"], [], ["own", "missing"]])
async def test_rejected_write_never_reaches_queue(async_db: Any, memories: None, monkeypatch: pytest.MonkeyPatch, ids: list[str]) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("denied authorization reached storage/queue")

    monkeypatch.setattr(service, "queue_save_to_memory_task", forbidden)
    assert await service.add_message(async_db, ids, {}, user_id="user") == (False, "Memory not found.")


async def test_write_deduplicates_before_queue(async_db: Any, memories: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def queue(db: Any, ids: list[str], message: dict) -> tuple[bool, str]:
        assert ids == ["own", "team"]
        return True, "queued"

    monkeypatch.setattr(service, "queue_save_to_memory_task", queue)
    assert await service.add_message(async_db, ["own,team", "own"], {}, user_id="user") == (True, "queued")


@pytest.mark.parametrize("key", ["tenant_llm_id", "tenant_embd_id"])
def test_raw_model_row_ids_rejected_before_cache(db: Session, memories: None, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setattr(service, "get_memory_size_cache", lambda *args: pytest.fail("invalid config reached storage"))
    with pytest.raises(ArgumentException, match="Use"):
        service.update_memory(db, "own", {key: 123}, user_id="user")
    with pytest.raises(NotFoundException):
        service.update_memory(db, "private", {key: 123}, user_id="user")


@pytest.mark.parametrize("path", ["embedding", "search"])
def test_pipeline_model_id_binds_memory_owner(db: Session, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    def scoped_lookup(db: Session, model_id: int, *, tenant_id: str, model_type: str) -> dict:
        assert model_id == 123 and tenant_id == "owner"
        assert model_type == "embedding"
        raise LookupError("scoped denial")

    monkeypatch.setattr(mms, "get_model_config_by_id", scoped_lookup)
    memory = Memory(id="own", tenant_id="owner", tenant_embd_id=123)
    if path == "embedding":
        with pytest.raises(LookupError, match="scoped denial"):
            mms._embedding_bundle_for(db, memory)
    elif path == "search":
        monkeypatch.setattr(MemoryService, "get_by_ids", lambda *args: [memory])
        with pytest.raises(LookupError, match="scoped denial"):
            mms.query_message(db, {"memory_id": ["own"]}, {"query": "test"})


async def test_extraction_model_id_binds_owner(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def scoped_lookup(db: Session, model_id: int, *, tenant_id: str, model_type: str) -> dict:
        assert (model_id, tenant_id) == (123, "owner")
        assert model_type == "chat"
        raise LookupError("scoped denial")

    monkeypatch.setattr(mms, "resolve_model_type", lambda *args: ["chat"])
    monkeypatch.setattr(mms, "get_model_config_by_id", scoped_lookup)
    with pytest.raises(LookupError, match="scoped denial"):
        await mms.extract_by_llm(db, "owner", "model", 123, {}, ["raw"], "hello", "world", system_prompt="s", user_prompt="u")


def test_model_config_denial_uses_scoped_sql_lookup(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def lookup(db: Session, **filters: Any) -> None:
        assert filters == {"id": 123, "tenant_id": "owner"}
        return None

    monkeypatch.setattr(models.TenantLLMService, "get_or_none", lookup)
    with pytest.raises(LookupError):
        models.get_model_config_by_id(db, 123, tenant_id="owner")


@pytest.mark.parametrize("operation", ["update", "delete", "config", "list", "content", "forget", "status"])
def test_direct_denial_precedes_storage(db: Session, memories: None, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("denied authorization reached storage")

    monkeypatch.setattr(service, "get_memory_size_cache", forbidden)
    for method in ["has_index", "delete_message", "list_message", "update_message", "get_by_message_id"]:
        monkeypatch.setattr(service.MessageService, method, forbidden)
    calls = {
        "update": lambda: service.update_memory(db, "private", {"description": "x"}, user_id="user"),
        "delete": lambda: service.delete_memory(db, "private", user_id="user"),
        "config": lambda: service.get_memory_config(db, "private", user_id="user"),
        "list": lambda: service.get_memory_messages(db, "private", [], "", user_id="user"),
        "content": lambda: service.get_message_content(db, "private", 1, user_id="user"),
        "forget": lambda: service.forget_message(db, "private", 1, user_id="user"),
        "status": lambda: service.update_message_status(db, "private", 1, False, user_id="user"),
    }
    with pytest.raises(NotFoundException):
        calls[operation]()
