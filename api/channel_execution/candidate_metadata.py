"""Execution-scoped ownership metadata for private Canvas candidates.

The Canvas completion service owns creation of new MultiRAG conversations. A
single, process-wide ``before_flush`` observer consumes request-local state from
``Session.info`` so that the conversation and its ownership row are committed
atomically without adding Channel parameters to the upstream-facing service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import event, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from api.db.db_models import API4Conversation, ChannelCanvasCandidate
from common.app_config import get_app_config
from common.misc_utils import get_uuid

CANVAS_CANDIDATE_LEGACY_NAME = "[channel-candidate]"
CANVAS_CANDIDATE_STATE_ACTIVE = "active"
CANVAS_CANDIDATE_STATE_FINALIZING = "finalizing"

_NEW_CANVAS_CANDIDATE_INFO_KEY = "multirag.channel.canvas_candidate"


@dataclass(slots=True)
class _NewCanvasCandidateCapture:
    owner_token: str
    target_id: str
    expires_at: datetime | ColumnElement[datetime]
    candidate_session_id: str | None = None
    committed: bool = False


def candidate_expires_at(*, max_age_seconds: int | None = None) -> ColumnElement[datetime]:
    """Return the absolute safety-net expiry for one execution candidate."""

    if max_age_seconds is None:
        max_age_seconds = get_app_config().channels.execution.candidate_gc.max_age_seconds
    return func.now() + timedelta(seconds=max_age_seconds)


def arm_new_canvas_candidate_capture(
    db: AsyncSession,
    *,
    owner_token: str,
    target_id: str,
    expires_at: datetime | ColumnElement[datetime] | None = None,
) -> None:
    """Arm one AsyncSession for the next core-created Canvas conversation."""

    info = db.sync_session.info
    if _NEW_CANVAS_CANDIDATE_INFO_KEY in info:
        raise RuntimeError("Canvas candidate capture is already armed")
    info[_NEW_CANVAS_CANDIDATE_INFO_KEY] = _NewCanvasCandidateCapture(
        owner_token=owner_token,
        target_id=target_id,
        expires_at=candidate_expires_at() if expires_at is None else expires_at,
    )


def clear_new_canvas_candidate_capture(
    db: AsyncSession,
    *,
    owner_token: str,
) -> None:
    """Disarm only the execution that owns the request-local capture."""

    info = db.sync_session.info
    capture = info.get(_NEW_CANVAS_CANDIDATE_INFO_KEY)
    if isinstance(capture, _NewCanvasCandidateCapture) and capture.owner_token == owner_token:
        info.pop(_NEW_CANVAS_CANDIDATE_INFO_KEY, None)


def _matching_new_conversations(
    session: Session,
    capture: _NewCanvasCandidateCapture,
) -> list[API4Conversation]:
    return [row for row in session.new if isinstance(row, API4Conversation) and row.dialog_id == capture.target_id and row.source == "agent"]


@event.listens_for(Session, "before_flush")
def _capture_new_canvas_candidate(
    session: Session,
    _flush_context: object,
    _instances: object,
) -> None:
    """Attach ownership metadata to the core insert in the same flush."""

    capture = session.info.get(_NEW_CANVAS_CANDIDATE_INFO_KEY)
    if not isinstance(capture, _NewCanvasCandidateCapture):
        return

    conversations = _matching_new_conversations(session, capture)
    if capture.committed:
        if conversations:
            raise RuntimeError("Committed Canvas candidate capture cannot be reused")
        return
    if capture.candidate_session_id is not None:
        if conversations:
            raise RuntimeError("Canvas candidate capture may be consumed only once")
        return
    if not conversations:
        return
    if len(conversations) != 1:
        raise RuntimeError("Canvas candidate capture requires exactly one conversation")

    conversation = conversations[0]
    if not conversation.id or conversation.user_id is None:
        raise RuntimeError("Canvas candidate conversation has no publish identity")

    publish_user_id = conversation.user_id
    publish_exp_user_id = conversation.exp_user_id
    publish_name = conversation.name
    conversation.name = CANVAS_CANDIDATE_LEGACY_NAME
    conversation.dialog_id = conversation.id
    conversation.user_id = conversation.id
    conversation.exp_user_id = conversation.id

    capture.candidate_session_id = conversation.id
    session.add(
        ChannelCanvasCandidate(
            id=get_uuid(),
            candidate_session_id=conversation.id,
            owner_token=capture.owner_token,
            target_id=capture.target_id,
            public_session_id=None,
            source_fingerprint=None,
            state=CANVAS_CANDIDATE_STATE_ACTIVE,
            expires_at=capture.expires_at,
            publish_user_id=publish_user_id,
            publish_exp_user_id=publish_exp_user_id,
            publish_name=publish_name,
        )
    )


@event.listens_for(Session, "after_commit")
def _mark_new_canvas_candidate_committed(session: Session) -> None:
    capture = session.info.get(_NEW_CANVAS_CANDIDATE_INFO_KEY)
    if isinstance(capture, _NewCanvasCandidateCapture) and capture.candidate_session_id is not None:
        capture.committed = True


@event.listens_for(Session, "after_soft_rollback")
def _reset_new_canvas_candidate_after_rollback(
    session: Session,
    _previous_transaction: object,
) -> None:
    capture = session.info.get(_NEW_CANVAS_CANDIDATE_INFO_KEY)
    if isinstance(capture, _NewCanvasCandidateCapture) and not capture.committed:
        capture.candidate_session_id = None
