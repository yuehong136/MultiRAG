"""Transactional history contracts for Channel-owned MultiRAG sessions."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, null, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import object_session

from api.channel_execution.errors import TargetExecutionFailedError
from api.channel_execution.executors import SqlAlchemyDialogTargetDriver
from api.channel_execution.history import SqlAlchemyCanvasHistoryTransaction, SqlAlchemyDialogHistoryTransaction
from api.channel_execution.models import ExecutionOperation
from api.db.db_models import API4Conversation, Conversation, Dialog


def _dialog_messages() -> list[dict[str, str]]:
    return [
        {"role": "assistant", "content": "prologue"},
        {"role": "user", "content": "previous", "id": "turn-1"},
        {
            "role": "assistant",
            "content": "<think>old private</think>previous answer",
            "id": "turn-1",
        },
        {"role": "user", "content": "same question", "id": "turn-2"},
        {
            "role": "assistant",
            "content": "<think>replaced private</think>old answer",
            "id": "turn-2",
        },
    ]


def _safe_canvas_dsl() -> dict[str, object]:
    return {
        "components": {
            "begin": {"obj": {"component_name": "Begin", "params": {}}},
            "llm": {"obj": {"component_name": "LLM", "params": {}}},
        },
        "history": [],
        "globals": {},
    }


@pytest.mark.parametrize(
    ("operation", "question"),
    [
        ("message", "follow up"),
        ("regenerate", "same question"),
    ],
    ids=["ordinary-message", "regenerate"],
)
async def test_dialog_existing_session_commits_one_detached_cas_without_candidate_dml(
    bootstrapped_async_engine: AsyncEngine,
    operation: ExecutionOperation,
    question: str,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    operation_id = "msg" if operation == "message" else "regen"
    public_id = f"dialog-dml-{operation_id}-00000000000001"
    original = _dialog_messages()
    conversation_dml: list[str] = []

    def _capture_conversation_dml(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.upper().split())
        if "USR_AI.T_AI_CONVERSATIONS" in normalized and normalized.startswith(("INSERT", "UPDATE", "DELETE")):
            conversation_dml.append(normalized.split(maxsplit=1)[0])

    event.listen(bootstrapped_async_engine.sync_engine, "before_cursor_execute", _capture_conversation_dml)
    try:
        async with factory() as db:
            db.add(
                Conversation(
                    id=public_id,
                    dialog_id="dialog-1",
                    name="public",
                    message=deepcopy(original),
                    reference=[{"chunks": ["previous"]}, {"chunks": ["old"]}],
                    user_id="user-1",
                )
            )
            await db.commit()
            conversation_dml.clear()

            transaction = SqlAlchemyDialogHistoryTransaction(db)
            prepared = await transaction.prepare(
                target_id="dialog-1",
                session_id=public_id,
                question=question,
                operation=operation,
            )
            if operation == "regenerate":
                assert [message["content"] for message in prepared.working_copy.message] == [
                    "prologue",
                    "previous",
                    "previous answer",
                ]
            else:
                assert prepared.working_copy.message[-1]["content"] == "old answer"
            public = await db.get(Conversation, public_id)
            assert public is not None and public.message == original
            assert conversation_dml == []

            prepared.working_copy.message.extend(
                [
                    {"role": "user", "content": question, "id": "turn-new"},
                    {
                        "role": "assistant",
                        "content": "<think>new private</think>new answer",
                        "id": "turn-new",
                    },
                ]
            )
            prepared.working_copy.reference.append({"chunks": ["new"]})

            assert await transaction.commit(prepared) == public_id
            assert conversation_dml == ["UPDATE"]

            public = await db.get(Conversation, public_id)
            assert public is not None
            assert [message["content"] for message in public.message].count(question) == 1
            assert public.message[-2]["content"] == question
            assert public.message[-1]["content"] == "new answer"
    finally:
        event.remove(bootstrapped_async_engine.sync_engine, "before_cursor_execute", _capture_conversation_dml)


async def test_dialog_two_detached_runs_from_same_head_allow_only_one_commit(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    public_id = "channel-dialog-public-0000000002"

    async with factory() as setup_db:
        setup_db.add(
            Conversation(
                id=public_id,
                dialog_id="dialog-2",
                name="public",
                message=_dialog_messages(),
                reference=[{"chunks": []}, {"chunks": []}],
                user_id="user-2",
            )
        )
        await setup_db.commit()

    async with factory() as first_db, factory() as second_db:
        first_transaction = SqlAlchemyDialogHistoryTransaction(first_db)
        second_transaction = SqlAlchemyDialogHistoryTransaction(second_db)
        first = await first_transaction.prepare(
            target_id="dialog-2",
            session_id=public_id,
            question="first concurrent question",
            operation="message",
        )
        second = await second_transaction.prepare(
            target_id="dialog-2",
            session_id=public_id,
            question="second concurrent question",
            operation="message",
        )
        first.working_copy.message.extend(
            [
                {"role": "user", "content": "first concurrent question", "id": "first"},
                {"role": "assistant", "content": "first answer", "id": "first"},
            ]
        )
        first.working_copy.reference.append({"chunks": ["first"]})
        second.working_copy.message.extend(
            [
                {"role": "user", "content": "second concurrent question", "id": "second"},
                {"role": "assistant", "content": "second answer", "id": "second"},
            ]
        )
        second.working_copy.reference.append({"chunks": ["second"]})

        await first_transaction.commit(first)
        with pytest.raises(TargetExecutionFailedError):
            await second_transaction.commit(second)
        await second_transaction.abort(second)

    async with factory() as verification_db:
        public = await verification_db.get(Conversation, public_id)
        assert public is not None
        assert public.message[-1]["content"] == "first answer"
        assert all(message.get("content") != "second answer" for message in public.message)


async def test_dialog_new_session_exists_only_after_terminal_commit(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)

    async with factory() as db:
        transaction = SqlAlchemyDialogHistoryTransaction(db)
        discarded = await transaction.prepare(
            target_id="dialog-new",
            session_id=None,
            question="discard me",
            operation="message",
            user_id="user-new",
        )
        assert await db.get(Conversation, discarded.public_session_id) is None
        await transaction.abort(discarded)
        assert await db.get(Conversation, discarded.public_session_id) is None

        prepared = await transaction.prepare(
            target_id="dialog-new",
            session_id=None,
            question="hello",
            operation="message",
            user_id="user-new",
        )
        prepared.working_copy.message.extend(
            [
                {"role": "assistant", "content": "prologue"},
                {"role": "user", "content": "hello", "id": "new-turn"},
                {"role": "assistant", "content": "answer", "id": "new-turn"},
            ]
        )
        prepared.working_copy.reference.append({"chunks": []})
        assert await db.get(Conversation, prepared.public_session_id) is None

        await transaction.commit(prepared)
        created = await db.get(Conversation, prepared.public_session_id)
        assert created is not None
        assert created.message[-1]["content"] == "answer"
        assert created.user_id == "user-new"


async def test_dialog_reasoning_only_result_keeps_public_head_unchanged(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    public_id = "dialog-reasoning-000000000000001"
    original_messages = _dialog_messages()
    original_references = [{"chunks": []}, {"chunks": []}]

    async with factory() as db:
        db.add(
            Conversation(
                id=public_id,
                dialog_id="dialog-reasoning",
                name="public",
                message=deepcopy(original_messages),
                reference=deepcopy(original_references),
                user_id="user-reasoning",
            )
        )
        await db.commit()
        transaction = SqlAlchemyDialogHistoryTransaction(db)
        prepared = await transaction.prepare(
            target_id="dialog-reasoning",
            session_id=public_id,
            question="hidden answer",
            operation="message",
        )
        prepared.working_copy.message.extend(
            [
                {"role": "user", "content": "hidden answer", "id": "hidden"},
                {"role": "assistant", "content": "<think>private only</think>", "id": "hidden"},
            ]
        )
        prepared.working_copy.reference.append({"chunks": []})

        with pytest.raises(TargetExecutionFailedError):
            await transaction.commit(prepared)
        await transaction.abort(prepared)

        public = await db.get(Conversation, public_id)
        assert public is not None
        assert public.message == original_messages
        assert public.reference == original_references


@pytest.mark.parametrize("sql_null_history", [False, True], ids=["json-null", "sql-null"])
async def test_dialog_cas_accepts_legacy_null_json_storage(
    bootstrapped_async_engine: AsyncEngine,
    sql_null_history: bool,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    public_id = f"dialog-null-{int(sql_null_history):020d}"

    async with factory() as db:
        db.add(
            Conversation(
                id=public_id,
                dialog_id="dialog-null",
                name="public",
                message=None,
                reference=None,
                user_id="user-null",
            )
        )
        await db.commit()
        if sql_null_history:
            await db.execute(update(Conversation).where(Conversation.id == public_id).values(message=null(), reference=null()))
            await db.commit()

        transaction = SqlAlchemyDialogHistoryTransaction(db)
        prepared = await transaction.prepare(
            target_id="dialog-null",
            session_id=public_id,
            question="hello",
            operation="message",
        )
        prepared.working_copy.message.extend(
            [
                {"role": "user", "content": "hello", "id": "null-turn"},
                {"role": "assistant", "content": "answer", "id": "null-turn"},
            ]
        )
        prepared.working_copy.reference.append({"chunks": []})

        await transaction.commit(prepared)
        public = await db.get(Conversation, public_id)
        assert public is not None
        assert public.message[-1]["content"] == "answer"


async def test_dialog_driver_uses_a_detached_target_configuration_snapshot(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    dialog_id = "dialog-snapshot-0000000000000001"

    async with factory() as db:
        db.add(
            Dialog(
                id=dialog_id,
                tenant_id="tenant-snapshot",
                name="snapshot",
                llm_id="factory@model",
                llm_setting={"max_tokens": 512},
                prompt_config={"system": "original", "prologue": "hello", "parameters": []},
                kb_ids=[],
                status="1",
            )
        )
        await db.commit()
        driver = SqlAlchemyDialogTargetDriver(db)

        snapshot = await driver._load_dialog_snapshot(
            tenant_id="tenant-snapshot",
            target_id=dialog_id,
        )
        assert object_session(snapshot) is None
        snapshot.prompt_config["system"] = "mutated in memory"

        persisted = await db.get(Dialog, dialog_id)
        assert persisted is not None
        assert persisted.prompt_config["system"] == "original"


async def test_canvas_regenerate_projects_visible_history_before_execution(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    public_id = "channel-canvas-public-0000000001"

    async with factory() as db:
        db.add(
            API4Conversation(
                id=public_id,
                name="public",
                dialog_id="canvas-1",
                user_id="user-1",
                message=_dialog_messages()[1:],
                reference=[],
                source="agent",
                dsl=_safe_canvas_dsl(),
            )
        )
        await db.commit()
        transaction = SqlAlchemyCanvasHistoryTransaction(db)

        prepared = await transaction.prepare(
            target_id="canvas-1",
            session_id=public_id,
            question="same question",
            operation="regenerate",
        )
        assert prepared.execution_session_id is not None
        candidate = await db.get(API4Conversation, prepared.execution_session_id)
        assert candidate is not None
        assert candidate.user_id == candidate.id
        assert candidate.exp_user_id == candidate.id
        assert candidate.dsl["history"] == [
            ["user", "previous"],
            ["assistant", "previous answer"],
        ]

        candidate.message = [
            *candidate.message,
            *[
                {"role": "user", "content": "same question", "id": "turn-new"},
                {
                    "role": "assistant",
                    "content": "<think>new private</think>new canvas answer",
                    "id": "turn-new",
                },
            ],
        ]
        await db.commit()
        await transaction.commit(prepared, candidate.id)

        public = await db.get(API4Conversation, public_id)
        assert public is not None
        assert public.message[-1]["content"] == "new canvas answer"
        assert public.dsl["history"][-1] == ["assistant", "new canvas answer"]
        assert await db.get(API4Conversation, candidate.id) is None


async def test_dialog_prepare_leaves_legacy_candidate_cleanup_outside_hot_path(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    expired_id = "expired-candidate-00000000000001"
    active_id = "active-candidate-000000000000001"
    expired_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
    expired_ms = int(expired_at.timestamp() * 1000)

    async with factory() as db:
        expired = Conversation(
            id=expired_id,
            dialog_id="dialog-gc",
            name="[channel-candidate]",
            message=[],
            reference=[],
            user_id=expired_id,
        )
        active = Conversation(
            id=active_id,
            dialog_id="dialog-gc",
            name="[channel-candidate]",
            message=[],
            reference=[],
            user_id=active_id,
        )
        db.add_all([expired, active])
        await db.commit()
        expired.update_date = expired_at
        expired.update_time = expired_ms
        await db.commit()

        transaction = SqlAlchemyDialogHistoryTransaction(db)
        prepared = await transaction.prepare(
            target_id="dialog-gc",
            session_id=None,
            question="bootstrap",
            operation="message",
        )

        assert prepared.public_session_id

    async with factory() as verification_db:
        assert await verification_db.get(Conversation, expired_id) is not None
        assert await verification_db.get(Conversation, active_id) is not None
