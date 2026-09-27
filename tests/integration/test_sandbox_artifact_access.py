"""Sandbox file authorization uses a server-created binding, not message text."""

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from api.apps.services.sandbox_artifact_service import _artifact_accessible
from api.db.db_models import API4Conversation, User, UserCanvas
from core.utils.redis_conn import REDIS_CONN
from core.utils.sandbox_artifact_registry import get_artifact_binding, record_artifact_binding


async def test_artifact_is_bound_before_assistant_message_is_saved(bootstrapped_async_engine: AsyncEngine) -> None:
    owner_id = uuid4().hex
    other_id = uuid4().hex
    canvas_id = uuid4().hex
    session_id = uuid4().hex
    run_id = uuid4().hex
    filename = f"{uuid4().hex}.pdf"
    stolen_filename = f"{uuid4().hex}.pdf"
    binding_keys = [f"multirag:sandbox_artifact:{name}" for name in (filename, stolen_filename)]

    async with bootstrapped_async_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            async with AsyncSession(bind=connection) as db:
                db.add_all(
                    [
                        User(id=owner_id, nickname="Artifact owner", email=f"{owner_id}@example.test"),
                        User(id=other_id, nickname="Other user", email=f"{other_id}@example.test"),
                        UserCanvas(id=canvas_id, user_id=owner_id, title="Artifact canvas"),
                        API4Conversation(
                            id=session_id,
                            dialog_id=canvas_id,
                            user_id=owner_id,
                            source="agent",
                            message=[{"role": "user", "content": "Create a PDF."}],
                        ),
                    ]
                )
                await db.flush()

                assert record_artifact_binding(filename, owner_id, run_id, session_id)
                assert get_artifact_binding(filename) is not None
                # CodeExec has uploaded the object, but the assistant message is
                # not persisted until the stream ends.
                assert await _artifact_accessible(db, filename, owner_id, run_id, session_id)
                assert not await _artifact_accessible(db, filename, other_id, run_id, session_id)
                assert not await _artifact_accessible(db, filename, owner_id, uuid4().hex, session_id)
                assert not await _artifact_accessible(db, filename, owner_id, run_id, uuid4().hex)
                assert not await _artifact_accessible(db, filename, owner_id, None, session_id)

                # Even a saved assistant message repeating another user's URL
                # cannot forge that object's server-created binding.
                assert record_artifact_binding(stolen_filename, other_id, uuid4().hex, None)
                conversation = await db.get(API4Conversation, session_id)
                assert conversation is not None
                conversation.message = [{"role": "assistant", "content": f"[Other PDF](/api/v1/documents/artifact/{stolen_filename})"}]
                await db.flush()
                assert not await _artifact_accessible(db, stolen_filename, owner_id, run_id, session_id)
                assert not await _artifact_accessible(db, stolen_filename, owner_id, None, None)

                await db.delete(conversation)
                await db.flush()
                assert not await _artifact_accessible(db, filename, owner_id, run_id, session_id)
        finally:
            await transaction.rollback()
            for key in binding_keys:
                REDIS_CONN.delete(key)
