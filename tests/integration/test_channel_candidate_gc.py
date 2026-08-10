"""PostgreSQL contracts for bounded, multi-instance Channel candidate GC."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.channel_execution.candidate_gc import collect_expired_candidate_batch
from api.channel_execution.candidate_metadata import (
    CANVAS_CANDIDATE_LEGACY_NAME,
    CANVAS_CANDIDATE_STATE_ACTIVE,
    CANVAS_CANDIDATE_STATE_FINALIZING,
)
from api.db.db_models import API4Conversation, ChannelCanvasCandidate, Conversation


def _identifier() -> str:
    return uuid.uuid4().hex


def _canvas_candidate(
    candidate_id: str,
    *,
    exp_user_id: str | None = None,
) -> API4Conversation:
    return API4Conversation(
        id=candidate_id,
        name=CANVAS_CANDIDATE_LEGACY_NAME,
        dialog_id=_identifier(),
        user_id=candidate_id,
        exp_user_id=exp_user_id if exp_user_id is not None else candidate_id,
        message=[],
        reference=[],
        source="agent",
        dsl={},
    )


def _canvas_metadata(
    candidate_id: str,
    *,
    expires_at: datetime,
    state: str = CANVAS_CANDIDATE_STATE_ACTIVE,
) -> ChannelCanvasCandidate:
    return ChannelCanvasCandidate(
        id=_identifier(),
        candidate_session_id=candidate_id,
        owner_token=_identifier(),
        target_id=_identifier(),
        public_session_id=None,
        source_fingerprint=None,
        state=state,
        expires_at=expires_at,
        publish_user_id="publish-user",
        publish_exp_user_id=None,
        publish_name="public conversation",
    )


async def _collect_one(
    factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int,
) -> int:
    async with factory() as db:
        result = await collect_expired_candidate_batch(
            db,
            max_age_seconds=86_400,
            batch_size=batch_size,
        )
    return result.total


async def test_gc_shares_one_batch_budget_and_preserves_legacy_near_misses(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    explicit_id = _identifier()
    legacy_canvas_id = _identifier()
    legacy_dialog_id = _identifier()
    near_canvas_id = _identifier()
    near_dialog_id = _identifier()
    fresh_dialog_id = _identifier()
    all_canvas_ids = [explicit_id, legacy_canvas_id, near_canvas_id]
    all_dialog_ids = [legacy_dialog_id, near_dialog_id, fresh_dialog_id]
    expired_at = datetime.now(UTC) - timedelta(days=2)
    expired_ms = int(expired_at.timestamp() * 1_000)

    async with factory() as setup_db:
        setup_db.add_all(
            [
                _canvas_candidate(explicit_id),
                _canvas_metadata(
                    explicit_id,
                    expires_at=expired_at,
                    state=CANVAS_CANDIDATE_STATE_FINALIZING,
                ),
                _canvas_candidate(legacy_canvas_id),
                _canvas_candidate(near_canvas_id, exp_user_id="real-publisher"),
                Conversation(
                    id=legacy_dialog_id,
                    dialog_id=_identifier(),
                    name=CANVAS_CANDIDATE_LEGACY_NAME,
                    message=[],
                    reference=[],
                    user_id=legacy_dialog_id,
                ),
                Conversation(
                    id=near_dialog_id,
                    dialog_id=_identifier(),
                    name=CANVAS_CANDIDATE_LEGACY_NAME,
                    message=[],
                    reference=[],
                    user_id="real-user",
                ),
                Conversation(
                    id=fresh_dialog_id,
                    dialog_id=_identifier(),
                    name=CANVAS_CANDIDATE_LEGACY_NAME,
                    message=[],
                    reference=[],
                    user_id=fresh_dialog_id,
                ),
            ]
        )
        await setup_db.commit()
        await setup_db.execute(update(API4Conversation).where(API4Conversation.id.in_([legacy_canvas_id, near_canvas_id])).values(update_time=expired_ms))
        await setup_db.execute(update(Conversation).where(Conversation.id.in_([legacy_dialog_id, near_dialog_id])).values(update_time=expired_ms))
        await setup_db.commit()

    try:
        async with factory() as first_db:
            first = await collect_expired_candidate_batch(
                first_db,
                max_age_seconds=86_400,
                batch_size=2,
            )

        assert first.explicit_canvas == 1
        assert first.legacy_canvas == 1
        assert first.legacy_dialog == 0
        assert first.total == 2

        async with factory() as verification_db:
            assert await verification_db.get(API4Conversation, explicit_id) is None
            assert await verification_db.scalar(select(ChannelCanvasCandidate).where(ChannelCanvasCandidate.candidate_session_id == explicit_id)) is None
            assert await verification_db.get(API4Conversation, legacy_canvas_id) is None
            assert await verification_db.get(Conversation, legacy_dialog_id) is not None
            assert await verification_db.get(API4Conversation, near_canvas_id) is not None
            assert await verification_db.get(Conversation, near_dialog_id) is not None
            assert await verification_db.get(Conversation, fresh_dialog_id) is not None

        async with factory() as second_db:
            second = await collect_expired_candidate_batch(
                second_db,
                max_age_seconds=86_400,
                batch_size=10,
            )

        assert second.explicit_canvas == 0
        assert second.legacy_canvas == 0
        assert second.legacy_dialog == 1
        assert second.total == 1
    finally:
        async with factory() as cleanup_db:
            await cleanup_db.execute(delete(API4Conversation).where(API4Conversation.id.in_(all_canvas_ids)))
            await cleanup_db.execute(delete(Conversation).where(Conversation.id.in_(all_dialog_ids)))
            await cleanup_db.commit()


async def test_gc_replicas_skip_locked_candidates_and_take_disjoint_batches(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    locked_id = _identifier()
    unlocked_ids = [_identifier(), _identifier()]
    candidate_ids = [locked_id, *unlocked_ids]
    now = datetime.now(UTC)

    async with factory() as setup_db:
        for position, candidate_id in enumerate(candidate_ids):
            setup_db.add(_canvas_candidate(candidate_id))
            setup_db.add(
                _canvas_metadata(
                    candidate_id,
                    expires_at=now - timedelta(days=3) + timedelta(minutes=position),
                )
            )
        await setup_db.commit()

    try:
        async with factory() as blocker_db:
            await blocker_db.begin()
            locked = await blocker_db.scalar(select(API4Conversation).where(API4Conversation.id == locked_id).with_for_update())
            assert locked is not None

            replica_totals = await asyncio.wait_for(
                asyncio.gather(
                    _collect_one(factory, batch_size=1),
                    _collect_one(factory, batch_size=1),
                ),
                timeout=5,
            )
            assert replica_totals == [1, 1]

            async with factory() as verification_db:
                assert await verification_db.get(API4Conversation, locked_id) is not None
                for unlocked_id in unlocked_ids:
                    assert await verification_db.get(API4Conversation, unlocked_id) is None

            await blocker_db.rollback()

        assert await _collect_one(factory, batch_size=1) == 1
        async with factory() as verification_db:
            assert await verification_db.get(API4Conversation, locked_id) is None
            remaining = await verification_db.scalars(select(ChannelCanvasCandidate).where(ChannelCanvasCandidate.candidate_session_id.in_(candidate_ids)))
            assert remaining.all() == []
    finally:
        async with factory() as cleanup_db:
            await cleanup_db.execute(delete(API4Conversation).where(API4Conversation.id.in_(candidate_ids)))
            await cleanup_db.commit()
