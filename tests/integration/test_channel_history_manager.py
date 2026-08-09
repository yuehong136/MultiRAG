"""Transactional copy-on-write contracts for Channel-owned RAGFlow sessions."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from api.channel_execution.errors import TargetExecutionFailedError
from api.channel_execution.history import SqlAlchemyChannelSessionManager
from api.db.db_models import API4Conversation, Conversation


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
            "message": {"obj": {"component_name": "Message", "params": {}}},
        },
        "history": [],
        "globals": {},
    }


async def test_dialog_regenerate_promotes_candidate_atomically(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    public_id = "channel-dialog-public-0000000001"
    original = _dialog_messages()

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

        manager = SqlAlchemyChannelSessionManager(db)
        prepared = await manager.prepare_dialog(
            target_id="dialog-1",
            session_id=public_id,
            question="same question",
            operation="regenerate",
        )
        assert prepared.execution_session_id is not None
        public = await db.get(Conversation, public_id)
        candidate = await db.get(Conversation, prepared.execution_session_id)
        assert public is not None and public.message == original
        assert candidate is not None
        assert candidate.name == "[channel-candidate]"
        assert candidate.user_id == candidate.id
        assert [message["content"] for message in candidate.message] == [
            "prologue",
            "previous",
            "previous answer",
        ]

        candidate.message = [
            *candidate.message,
            *[
                {"role": "user", "content": "same question", "id": "turn-new"},
                {
                    "role": "assistant",
                    "content": "<think>new private</think>new answer",
                    "id": "turn-new",
                },
            ],
        ]
        candidate.reference = [*candidate.reference, {"chunks": ["new"]}]
        await db.commit()

        promoted_id = await manager.complete_dialog(
            prepared,
            candidate.id,
            require_visible_answer=True,
        )
        assert promoted_id == public_id

        public = await db.get(Conversation, public_id)
        assert public is not None
        assert [message["content"] for message in public.message].count("same question") == 1
        assert public.message[-1]["content"] == "new answer"
        assert await db.get(Conversation, candidate.id) is None


async def test_dialog_conflict_keeps_public_head_and_abort_removes_candidate(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    public_id = "channel-dialog-public-0000000002"

    async with factory() as db:
        db.add(
            Conversation(
                id=public_id,
                dialog_id="dialog-2",
                name="public",
                message=_dialog_messages(),
                reference=[{"chunks": []}, {"chunks": []}],
                user_id="user-2",
            )
        )
        await db.commit()
        manager = SqlAlchemyChannelSessionManager(db)
        prepared = await manager.prepare_dialog(
            target_id="dialog-2",
            session_id=public_id,
            question="same question",
            operation="regenerate",
        )
        assert prepared.execution_session_id is not None

        public = await db.get(Conversation, public_id)
        candidate = await db.get(Conversation, prepared.execution_session_id)
        assert public is not None and candidate is not None
        public.message = [*public.message, {"role": "system", "content": "newer head"}]
        candidate.message = [
            *candidate.message,
            *[
                {"role": "user", "content": "same question", "id": "turn-new"},
                {"role": "assistant", "content": "new answer", "id": "turn-new"},
            ],
        ]
        await db.commit()

        with pytest.raises(TargetExecutionFailedError):
            await manager.complete_dialog(
                prepared,
                candidate.id,
                require_visible_answer=True,
            )
        await manager.abort(prepared, candidate.id)

        public = await db.get(Conversation, public_id)
        assert public is not None and public.message[-1]["content"] == "newer head"
        assert await db.get(Conversation, candidate.id) is None


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
        manager = SqlAlchemyChannelSessionManager(db)

        prepared = await manager.prepare_canvas(
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
        await manager.complete_canvas(prepared, candidate.id)

        public = await db.get(API4Conversation, public_id)
        assert public is not None
        assert public.message[-1]["content"] == "new canvas answer"
        assert public.dsl["history"][-1] == ["assistant", "new canvas answer"]
        assert await db.get(API4Conversation, candidate.id) is None


async def test_prepare_prunes_only_expired_internal_candidates(
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

        manager = SqlAlchemyChannelSessionManager(db)
        prepared = await manager.prepare_dialog(
            target_id="dialog-gc",
            session_id=None,
            question="bootstrap",
            operation="message",
        )

        assert prepared.execution_session_id is None

    async with factory() as verification_db:
        assert await verification_db.get(Conversation, expired_id) is None
        assert await verification_db.get(Conversation, active_id) is not None
