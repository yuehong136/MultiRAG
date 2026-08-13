"""Transactional history contracts for Channel-owned MultiRAG sessions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import delete, event, null, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Session, object_session

from api.channel_execution.errors import TargetExecutionFailedError
from api.channel_execution.executors import SqlAlchemyDialogTargetDriver
from api.channel_execution.history import SqlAlchemyCanvasHistoryTransaction, SqlAlchemyDialogHistoryTransaction
from api.channel_execution.models import ExecutionOperation
from api.db.db_models import API4Conversation, ChannelCanvasCandidate, Conversation, Dialog
from api.db.services.api_service import API4ConversationService


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


def _listed_canvas_session_ids(sync_db: Session, target_id: str) -> set[str]:
    _total, rows = API4ConversationService.get_list(
        sync_db,
        target_id,
        "tenant-for-list-contract",
        1,
        100,
        "create_time",
        False,
        include_dsl=False,
    )
    return {str(row["id"]) for row in rows}


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
                user_id="user-1",
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
            user_id="user-2",
        )
        second = await second_transaction.prepare(
            target_id="dialog-2",
            session_id=public_id,
            question="second concurrent question",
            operation="message",
            user_id="user-2",
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


async def test_existing_dialog_session_is_owned_by_current_principal(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    public_id = "dialog-owner-000000000000000001"

    async with factory() as db:
        db.add(
            Conversation(
                id=public_id,
                dialog_id="dialog-owner-check",
                name="public",
                message=_dialog_messages(),
                reference=[{"chunks": []}, {"chunks": []}],
                user_id="principal-a",
            )
        )
        await db.commit()
        transaction = SqlAlchemyDialogHistoryTransaction(db)

        prepared = await transaction.prepare(
            target_id="dialog-owner-check",
            session_id=public_id,
            question="continue",
            operation="message",
            user_id="principal-a",
        )
        assert prepared.working_copy.user_id == "principal-a"
        await transaction.abort(prepared)

        with pytest.raises(LookupError, match="Session not found"):
            await transaction.prepare(
                target_id="dialog-owner-check",
                session_id=public_id,
                question="cross principal",
                operation="message",
                user_id="principal-b",
            )


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
            user_id="user-reasoning",
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
            user_id="user-null",
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
            user_id="user-1",
        )
        assert prepared.execution_session_id is not None
        candidate = await db.get(API4Conversation, prepared.execution_session_id)
        assert candidate is not None
        assert candidate.user_id == candidate.id
        assert candidate.exp_user_id == candidate.id
        metadata = (await db.scalars(select(ChannelCanvasCandidate).where(ChannelCanvasCandidate.candidate_session_id == candidate.id))).one()
        assert metadata.owner_token == prepared.owner_token
        assert metadata.target_id == prepared.target_id == "canvas-1"
        assert metadata.public_session_id == public_id
        assert candidate.id not in await db.run_sync(lambda sync_db: _listed_canvas_session_ids(sync_db, "canvas-1"))
        assert candidate.dsl["history"] == [
            ["user", "previous"],
            ["assistant", "previous answer"],
        ]

        # Presentation markers are not ownership credentials. The private
        # dialog scope keeps this row out of normal target list/delete paths.
        candidate.name = "marker-was-changed"
        candidate.user_id = "identity-was-changed"
        candidate.exp_user_id = None
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
        assert await db.get(ChannelCanvasCandidate, metadata.id) is None


async def test_canvas_new_session_is_registered_atomically_and_restores_publish_identity(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)

    async with factory() as db:
        transaction = SqlAlchemyCanvasHistoryTransaction(db)
        prepared = await transaction.prepare(
            target_id="canvas-new",
            session_id=None,
            question="hello",
            operation="message",
            user_id="principal-new",
        )

        def _core_save(sync_db: Session) -> str:
            candidate = API4ConversationService.save(
                sync_db,
                id="canvas-new-candidate-00000000001",
                name="publish title",
                dialog_id="canvas-new",
                user_id="principal-new",
                exp_user_id="external-new",
                message=[],
                reference=[],
                source="agent",
                dsl=_safe_canvas_dsl(),
            )
            return candidate.id

        candidate_id = await db.run_sync(_core_save)
        candidate = await db.get(API4Conversation, candidate_id)
        assert candidate is not None
        assert candidate.name == "[channel-candidate]"
        assert candidate.user_id == candidate.id
        assert candidate.exp_user_id == candidate.id

        metadata = (await db.scalars(select(ChannelCanvasCandidate).where(ChannelCanvasCandidate.owner_token == prepared.owner_token))).one()
        assert metadata.candidate_session_id == candidate_id
        assert metadata.publish_user_id == "principal-new"
        assert metadata.publish_exp_user_id == "external-new"
        assert metadata.publish_name == "publish title"
        assert candidate_id not in await db.run_sync(lambda sync_db: _listed_canvas_session_ids(sync_db, "canvas-new"))

        candidate.message = [
            {"role": "user", "content": "hello", "id": "new-turn"},
            {
                "role": "assistant",
                "content": "<think>private</think>published answer",
                "id": "new-turn",
            },
        ]
        await db.commit()

        assert await transaction.commit(prepared, candidate_id) == candidate_id
        published = await db.get(API4Conversation, candidate_id)
        assert published is not None
        assert published.name == "publish title"
        assert published.user_id == "principal-new"
        assert published.exp_user_id == "external-new"
        assert published.message[-1]["content"] == "published answer"
        assert await db.get(ChannelCanvasCandidate, metadata.id) is None
        assert candidate_id in await db.run_sync(lambda sync_db: _listed_canvas_session_ids(sync_db, "canvas-new"))


async def test_existing_canvas_session_is_owned_by_current_principal(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    public_id = "canvas-owner-000000000000000001"

    async with factory() as db:
        db.add(
            API4Conversation(
                id=public_id,
                name="public",
                dialog_id="canvas-owner-check",
                user_id="principal-a",
                message=_dialog_messages()[1:],
                reference=[],
                source="agent",
                dsl=_safe_canvas_dsl(),
            )
        )
        await db.commit()
        transaction = SqlAlchemyCanvasHistoryTransaction(db)

        prepared = await transaction.prepare(
            target_id="canvas-owner-check",
            session_id=public_id,
            question="continue",
            operation="message",
            user_id="principal-a",
        )
        assert prepared.expected_user_id == "principal-a"
        await transaction.abort(prepared, prepared.execution_session_id)

        with pytest.raises(LookupError, match="Session not found"):
            await transaction.prepare(
                target_id="canvas-owner-check",
                session_id=public_id,
                question="cross principal",
                operation="message",
                user_id="principal-b",
            )


async def test_canvas_owner_token_fences_commit_and_abort(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    public_id = "canvas-owner-public-00000000001"

    async with factory() as db:
        db.add(
            API4Conversation(
                id=public_id,
                name="public",
                dialog_id="canvas-owner",
                user_id="principal-owner",
                message=_dialog_messages()[1:],
                reference=[],
                source="agent",
                dsl=_safe_canvas_dsl(),
            )
        )
        await db.commit()
        transaction = SqlAlchemyCanvasHistoryTransaction(db)
        prepared = await transaction.prepare(
            target_id="canvas-owner",
            session_id=public_id,
            question="follow up",
            operation="message",
            user_id="principal-owner",
        )
        candidate_id = prepared.execution_session_id
        assert candidate_id is not None
        candidate = await db.get(API4Conversation, candidate_id)
        assert candidate is not None
        candidate.message = [
            *candidate.message,
            {"role": "user", "content": "follow up", "id": "owner-turn"},
            {"role": "assistant", "content": "answer", "id": "owner-turn"},
        ]
        await db.commit()

        forged = replace(prepared, owner_token="wrong-owner-token-000000000000")
        with pytest.raises(TargetExecutionFailedError):
            await transaction.commit(forged, candidate_id)
        await transaction.abort(forged, candidate_id)
        assert await db.get(API4Conversation, candidate_id) is not None
        assert await db.scalar(select(ChannelCanvasCandidate.id).where(ChannelCanvasCandidate.owner_token == prepared.owner_token)) is not None

        await transaction.abort(prepared, candidate_id)
        assert await db.get(API4Conversation, candidate_id) is None
        assert await db.get(API4Conversation, public_id) is not None


async def test_canvas_abort_finds_core_candidate_before_first_frame(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    candidate_id = "canvas-pre-frame-000000000000001"
    retry_candidate_id = "canvas-pre-frame-retry-000000001"

    async with factory() as db:
        transaction = SqlAlchemyCanvasHistoryTransaction(db)
        prepared = await transaction.prepare(
            target_id="canvas-pre-frame",
            session_id=None,
            question="hello",
            operation="message",
            user_id="principal-pre-frame",
        )

        def _commit_then_fail(sync_db: Session) -> None:
            sync_db.add(
                API4Conversation(
                    id=candidate_id,
                    dialog_id="canvas-pre-frame",
                    user_id="principal-pre-frame",
                    message=[],
                    reference=[],
                    source="agent",
                    dsl=_safe_canvas_dsl(),
                )
            )
            sync_db.commit()
            raise RuntimeError("simulated refresh failure after commit")

        with pytest.raises(RuntimeError, match="refresh failure"):
            await db.run_sync(_commit_then_fail)
        assert await db.get(API4Conversation, candidate_id) is not None
        assert await db.scalar(select(ChannelCanvasCandidate.id).where(ChannelCanvasCandidate.owner_token == prepared.owner_token)) is not None

        def _core_retry(sync_db: Session) -> None:
            sync_db.add(
                API4Conversation(
                    id=retry_candidate_id,
                    dialog_id="canvas-pre-frame",
                    user_id="principal-pre-frame",
                    message=[],
                    reference=[],
                    source="agent",
                    dsl=_safe_canvas_dsl(),
                )
            )
            sync_db.commit()

        with pytest.raises(RuntimeError, match="cannot be reused"):
            await db.run_sync(_core_retry)

        await transaction.abort(prepared, None)
        assert await db.get(API4Conversation, candidate_id) is None
        assert await db.get(API4Conversation, retry_candidate_id) is None
        assert await db.scalar(select(ChannelCanvasCandidate.id).where(ChannelCanvasCandidate.owner_token == prepared.owner_token)) is None


async def test_canvas_core_insert_failure_leaves_no_unowned_candidate(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    candidate_id = "canvas-insert-fail-0000000000001"

    async with factory() as db:
        transaction = SqlAlchemyCanvasHistoryTransaction(db)
        prepared = await transaction.prepare(
            target_id="canvas-insert-fail",
            session_id=None,
            question="hello",
            operation="message",
        )

        def _invalid_core_insert(sync_db: Session) -> None:
            sync_db.add(
                API4Conversation(
                    id=candidate_id,
                    dialog_id="canvas-insert-fail",
                    user_id=None,
                    message=[],
                    reference=[],
                    source="agent",
                    dsl=_safe_canvas_dsl(),
                )
            )
            sync_db.commit()

        with pytest.raises(RuntimeError, match="publish identity"):
            await db.run_sync(_invalid_core_insert)
        await transaction.abort(prepared, None)
        assert await db.get(API4Conversation, candidate_id) is None
        assert await db.scalar(select(ChannelCanvasCandidate.id).where(ChannelCanvasCandidate.owner_token == prepared.owner_token)) is None


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
        try:
            assert await verification_db.get(Conversation, expired_id) is not None
            assert await verification_db.get(Conversation, active_id) is not None
        finally:
            await verification_db.rollback()
            await verification_db.execute(delete(Conversation).where(Conversation.id.in_([expired_id, active_id])))
            await verification_db.commit()


async def test_canvas_public_table_has_no_candidate_metadata_columns(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    async with bootstrapped_async_engine.connect() as connection:

        def _column_names(sync_connection: sa.Connection) -> set[str]:
            inspector = sa.inspect(sync_connection)
            return {
                column["name"]
                for column in inspector.get_columns(
                    API4Conversation.__tablename__,
                    schema="usr_ai",
                )
            }

        columns = await connection.run_sync(_column_names)

    assert not columns.intersection(
        {
            "candidate_session_id",
            "owner_token",
            "public_session_id",
            "source_fingerprint",
            "state",
            "expires_at",
            "publish_user_id",
            "publish_exp_user_id",
            "publish_name",
        }
    )


def test_channel_canvas_candidate_migration_matches_orm(
    pg_scratch_engine: sa.Engine,
    alembic_cfg: Any,
) -> None:
    revision = ScriptDirectory.from_config(alembic_cfg).get_revision("e4f6a8b0c2d4")
    assert revision is not None
    migration = revision.module
    schema = "ccm_migration"
    original_schema = migration.SCHEMA
    original_op = migration.op

    try:
        with pg_scratch_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(sa.text(f'CREATE TABLE "{schema}"."t_ai_api4conversations" (id VARCHAR(32) PRIMARY KEY)'))
            migration.SCHEMA = schema
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            inspector = sa.inspect(connection)
            table = ChannelCanvasCandidate.__table__
            columns = {column["name"]: column for column in inspector.get_columns(migration.TABLE, schema=schema)}
            assert set(columns) == {column.name for column in table.columns}
            for column in table.columns:
                assert columns[column.name]["nullable"] is column.nullable
                expected_length = getattr(column.type, "length", None)
                if expected_length is not None:
                    assert columns[column.name]["type"].length == expected_length

            assert {
                constraint["name"]
                for constraint in inspector.get_unique_constraints(
                    migration.TABLE,
                    schema=schema,
                )
            } == {
                "uq_channel_canvas_candidates_owner",
                "uq_channel_canvas_candidates_session",
            }
            assert {
                constraint["name"]
                for constraint in inspector.get_check_constraints(
                    migration.TABLE,
                    schema=schema,
                )
            } == {
                "ck_channel_canvas_candidates_distinct_sessions",
                "ck_channel_canvas_candidates_identity",
                "ck_channel_canvas_candidates_state",
            }
            indexes = {index["name"]: tuple(index["column_names"]) for index in inspector.get_indexes(migration.TABLE, schema=schema) if not index.get("duplicates_constraint")}
            assert indexes == {
                "ix_channel_canvas_candidates_state_expiry": (
                    "state",
                    "expires_at",
                    "candidate_session_id",
                ),
                f"ix_{schema}_t_ai_channel_canvas_candidates_create_date": ("create_date",),
                f"ix_{schema}_t_ai_channel_canvas_candidates_create_time": ("create_time",),
                f"ix_{schema}_t_ai_channel_canvas_candidates_update_date": ("update_date",),
                f"ix_{schema}_t_ai_channel_canvas_candidates_update_time": ("update_time",),
            }
            foreign_keys = inspector.get_foreign_keys(migration.TABLE, schema=schema)
            assert len(foreign_keys) == 1
            assert foreign_keys[0]["constrained_columns"] == ["candidate_session_id"]
            assert foreign_keys[0]["referred_table"] == "t_ai_api4conversations"
            assert foreign_keys[0]["options"].get("ondelete") == "CASCADE"

            migration.downgrade()
            assert not sa.inspect(connection).has_table(migration.TABLE, schema=schema)
            connection.execute(sa.text(f'CREATE TABLE "{schema}"."{migration.TABLE}" (id VARCHAR(32) PRIMARY KEY)'))
            with pytest.raises(RuntimeError, match="incompatible columns"):
                migration.upgrade()
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        migration.SCHEMA = original_schema
        migration.op = original_op
