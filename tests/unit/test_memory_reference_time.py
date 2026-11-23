"""Extraction dates use the persisted RAW receipt rather than worker execution time."""

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.db.db_models import Memory, Task
from api.db.joint_services import memory_message_service as mms
from common.constants import MemoryType
from memory.utils.prompt_util import PromptAssembler


@pytest.fixture
def extraction_source(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    memory = Memory(id="memory", tenant_id="owner", memory_type=5, llm_id="chat", temperature=0.1)
    raw = {
        "message_id": 42,
        "memory_id": "memory",
        "message_type": "raw",
        "source_id": 0,
        "user_id": "subject",
        "agent_id": "agent",
        "session_id": "session",
        "valid_at": "2026-11-01 10:00:00",
        "content": "User Input: 明天15:00到16:00开会\nAgent Response: 已记录",
    }
    message = {"user_id": "subject", "agent_id": "agent", "session_id": "session", "user_input": "forged queue text", "agent_response": "forged queue reply"}
    reads: list[tuple[str, int, str]] = []
    extracted: list[dict[str, Any]] = []
    writes: list[list[dict[str, Any]]] = []

    def read(memory_id: str, message_id: int, tenant_id: str) -> dict[str, Any]:
        reads.append((memory_id, message_id, tenant_id))
        return raw

    async def extract(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        extracted.append(kwargs)
        return [{"message_type": "episodic", "content": "会议", "valid_at": "2026-11-02 15:00:00", "invalid_at": "2026-11-02 16:00:00"}]

    async def save(db: Session, memory: Memory, messages: list[dict[str, Any]], task_id: str | None = None) -> tuple[bool, str]:
        writes.append(messages)
        return True, "saved"

    monkeypatch.setattr(mms.MemoryService, "get_by_memory_id", lambda db, mid: memory if mid == "memory" else None)

    def read_sources(tenant_id: str, memory_id: str, message_ids: list[int]) -> dict[int, dict[str, Any]]:
        assert len(message_ids) == 1
        result = read(memory_id, message_ids[0], tenant_id)
        return {message_ids[0]: result} if result else {}

    monkeypatch.setattr(mms.MessageService, "get_source_messages", read_sources)
    monkeypatch.setattr(mms, "extract_by_llm", extract)
    monkeypatch.setattr(mms, "embed_and_save", save)
    monkeypatch.setattr(mms.REDIS_CONN, "generate_auto_increment_id", lambda **kwargs: 43)
    monkeypatch.setattr(mms.TaskService, "update_progress", lambda *args: None)
    return {"raw": raw, "memory": memory, "message": message, "reads": reads, "extracted": extracted, "writes": writes}


async def test_legacy_task_retries_use_stored_reference_and_content(db: Session, extraction_source: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = extraction_source
    task = Task(id="task", doc_id="memory", digest="42", task_type="memory", progress=0)
    monkeypatch.setattr(mms.TaskService, "get_by_id", lambda db, tid: task)
    monkeypatch.setattr(mms.TaskService, "mark_started", lambda *args: None)
    payload = {"id": "task", "memory_id": "memory", "tenant_id": "owner", "source_id": 42, "message_dict": env["message"]}
    for worker_time in (datetime(2026, 11, 5, tzinfo=UTC), datetime(2026, 12, 9, tzinfo=UTC)):
        monkeypatch.setattr(mms, "current_timestamp", lambda now=worker_time: int(now.timestamp() * 1000))
        assert await mms.handle_save_to_memory_task(db, payload) == (True, "saved")

    assert env["reads"] == [("memory", 42, "owner"), ("memory", 42, "owner")]
    assert [call["conversation_time"] for call in env["extracted"]] == ["2026-11-01 10:00:00"] * 2
    assert [call["conversation_content"] for call in env["extracted"]] == [env["raw"]["content"]] * 2
    assert all(messages[0]["source_id"] == 42 for messages in env["writes"])
    assert all(messages[0]["invalid_at"] == "2026-11-02 16:00:00" for messages in env["writes"])
    assert "conversation_time" not in payload["message_dict"]


@pytest.mark.parametrize(
    "change",
    [
        {"valid_at": None},
        {"valid_at": ""},
        {"valid_at": "2026-11-01"},
        {"valid_at": "2026-02-30 10:00:00"},
        {"valid_at": 1793498400000},
        {"valid_at": True},
        {"memory_id": "foreign"},
        {"message_id": 99},
        {"message_type": "semantic"},
        {"source_id": 41},
        {"source_id": False},
        {"user_id": "foreign"},
        {"agent_id": "foreign"},
        {"session_id": "foreign"},
        {"content": None},
    ],
)
async def test_invalid_raw_source_stops_before_model_and_write(db: Session, extraction_source: dict[str, Any], change: dict[str, Any]) -> None:
    env = extraction_source
    env["raw"].update(change)
    success, message = await mms.save_extracted_to_memory_only(db, "memory", env["message"], 42)
    assert success is False
    assert "Source raw message" in message
    assert env["extracted"] == []
    assert env["writes"] == []


async def test_missing_raw_source_does_not_use_task_or_worker_time(db: Session, extraction_source: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = extraction_source
    monkeypatch.setattr(mms.MessageService, "get_source_messages", lambda *args: {})
    env["message"]["conversation_time"] = "2026-11-01 10:00:00"
    assert await mms.save_extracted_to_memory_only(db, "memory", env["message"], 42) == (False, "Source raw message was not found.")
    assert env["extracted"] == env["writes"] == []


@pytest.mark.parametrize("source_id", [None, 0, "0", ""])
async def test_normalized_raw_parent_is_accepted(db: Session, extraction_source: dict[str, Any], source_id: object) -> None:
    env = extraction_source
    env["raw"]["source_id"] = source_id
    assert await mms.save_extracted_to_memory_only(db, "memory", env["message"], 42) == (True, "saved")


@pytest.mark.parametrize("source_id", [False, True, 0.0, "00"])
async def test_invalid_normalized_raw_parent_is_rejected(db: Session, extraction_source: dict[str, Any], source_id: object) -> None:
    env = extraction_source
    env["raw"]["source_id"] = source_id
    assert await mms.save_extracted_to_memory_only(db, "memory", env["message"], 42) == (False, "Source raw message does not match the memory task.")
    assert env["extracted"] == env["writes"] == []


async def test_missing_raw_parent_is_rejected(db: Session, extraction_source: dict[str, Any]) -> None:
    env = extraction_source
    del env["raw"]["source_id"]
    assert await mms.save_extracted_to_memory_only(db, "memory", env["message"], 42) == (False, "Source raw message does not match the memory task.")
    assert env["extracted"] == env["writes"] == []


@pytest.mark.parametrize("field", ["user_id", "agent_id", "session_id"])
@pytest.mark.parametrize("value", [None, 0, False])
async def test_malformed_source_identity_does_not_equal_empty_subject(db: Session, extraction_source: dict[str, Any], field: str, value: object) -> None:
    env = extraction_source
    env["message"][field] = ""
    env["raw"][field] = value
    assert await mms.save_extracted_to_memory_only(db, "memory", env["message"], 42) == (False, "Source raw message does not match the memory task.")
    assert env["extracted"] == env["writes"] == []


@pytest.mark.parametrize("field", ["user_id", "agent_id", "session_id"])
async def test_missing_source_identity_is_rejected(db: Session, extraction_source: dict[str, Any], field: str) -> None:
    env = extraction_source
    env["message"][field] = ""
    del env["raw"][field]
    assert await mms.save_extracted_to_memory_only(db, "memory", env["message"], 42) == (False, "Source raw message does not match the memory task.")
    assert env["extracted"] == env["writes"] == []


@pytest.mark.parametrize("value", [None, 0, False])
async def test_malformed_task_subject_is_rejected(db: Session, extraction_source: dict[str, Any], value: object) -> None:
    env = extraction_source
    env["message"]["user_id"] = value
    env["raw"]["user_id"] = ""
    assert await mms.save_extracted_to_memory_only(db, "memory", env["message"], 42) == (False, "Source raw message does not match the memory task.")
    assert env["extracted"] == env["writes"] == []


@pytest.mark.parametrize("subject", ["", " ", " external subject "])
async def test_subject_identity_remains_exact(db: Session, extraction_source: dict[str, Any], subject: str) -> None:
    env = extraction_source
    env["message"]["user_id"] = env["raw"]["user_id"] = subject
    assert await mms.save_extracted_to_memory_only(db, "memory", env["message"], 42) == (True, "saved")
    assert env["writes"][0][0]["user_id"] == subject


async def test_legacy_task_without_subject_matches_only_empty_source_subject(db: Session, extraction_source: dict[str, Any]) -> None:
    env = extraction_source
    del env["message"]["user_id"]
    env["raw"]["user_id"] = ""
    assert await mms.save_extracted_to_memory_only(db, "memory", env["message"], 42) == (True, "saved")
    assert env["writes"][0][0]["user_id"] == ""


@pytest.mark.parametrize("source_id", [0, -1, True])
async def test_invalid_source_id_is_rejected_before_storage(db: Session, extraction_source: dict[str, Any], source_id: int) -> None:
    env = extraction_source
    assert await mms.save_extracted_to_memory_only(db, "memory", env["message"], source_id) == (False, "Source raw message does not match the memory task.")
    assert env["reads"] == env["extracted"] == env["writes"] == []


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("2026-11-01 10:00:00", "2026-11-01 10:00:00"),
        ("2026-11-01T10:00:00+08:00", "2026-11-01 10:00:00+08:00"),
        ("2026-11-01T02:00:00Z", "2026-11-01 02:00:00+00:00"),
        (datetime(2026, 11, 1, 10), "2026-11-01 10:00:00"),
        (datetime(2026, 11, 1, 10, tzinfo=timezone(timedelta(hours=8))), "2026-11-01 10:00:00+08:00"),
    ],
)
def test_reference_keeps_recorded_wall_time_and_timezone(stored: object, expected: str) -> None:
    assert mms._conversation_reference_time(stored) == expected


@pytest.mark.parametrize("custom_prompt", ["", "Extract episodic memories."])
async def test_chinese_relative_time_prompt_is_stable_and_keeps_event_end(db: Session, monkeypatch: pytest.MonkeyPatch, custom_prompt: str) -> None:
    prompts: list[list[dict[str, str]]] = []

    class ControlledModel:
        async def async_chat(self, system: str, messages: list[dict[str, str]], config: dict[str, Any]) -> str:
            prompts.append(messages)
            return json.dumps({"episodic": [{"content": "会议", "valid_at": "2026-11-02T15:00:00", "invalid_at": "2026-11-02T16:00:00"}]})

    monkeypatch.setattr(mms, "get_model_config_by_id", lambda *args, **kwargs: {})
    monkeypatch.setattr(mms, "LLMBundle", lambda *args: ControlledModel())
    for worker_time in (1, 9999999999999):
        monkeypatch.setattr(mms, "current_timestamp", lambda now=worker_time: now)
        result = await mms.extract_by_llm(
            db,
            "owner",
            "a" * 32,
            "a" * 32,
            {},
            ["episodic"],
            "明天15:00到16:00开会",
            "已记录",
            user_prompt=custom_prompt,
            conversation_time="2026-11-01 10:00:00",
        )
        assert result[0]["valid_at"] == "2026-11-02 15:00:00"
        assert result[0]["invalid_at"] == "2026-11-02 16:00:00"
    assert prompts[0] == prompts[1]
    content = prompts[0][-1]["content"]
    assert "**CONVERSATION TIME:** 2026-11-01 10:00:00" in content
    assert "**CURRENT TIME:** Not specified" in content
    assert "今天、昨天、明天、上周" in content
    assert "invalid_at remains the event end time" in content


def test_unspecified_reference_does_not_synthesize_clock_time() -> None:
    prompt = PromptAssembler.assemble_user_prompt("明天开会")
    assert "**CONVERSATION TIME:** Not specified" in prompt
    assert "**CURRENT TIME:** Not specified" in prompt
    assert "relative event dates are unknown" in prompt


async def test_synchronous_save_shares_one_receipt_time_with_raw(db: Session, extraction_source: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def now() -> int:
        calls.append(1)
        return 1793498400000

    monkeypatch.setattr(mms, "current_timestamp", now)
    env = extraction_source
    assert await mms.save_to_memory(db, "memory", env["message"]) == (True, "saved")
    assert env["writes"][0][0]["message_type"] == MemoryType.RAW.name.lower()
    assert env["writes"][0][0]["valid_at"] == env["extracted"][0]["conversation_time"]
    assert calls == [1]
