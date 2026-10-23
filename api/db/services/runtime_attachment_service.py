"""Session-scoped deletion of trusted runtime uploads, without a global GC job."""

import asyncio
import re
from typing import Any

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import API4Conversation, Conversation, Dialog
from api.db.services.file_service import FileService


class RuntimeAttachmentService:
    @staticmethod
    def upload_locations(messages: Any, owner_id: str) -> list[str]:
        """Treat persisted message metadata as hints, never as storage authority."""
        locations: dict[str, None] = {}
        for message in messages if isinstance(messages, list) else []:
            if not isinstance(message, dict) or not isinstance(message.get("files"), list):
                continue
            for file in message["files"]:
                if not isinstance(file, dict) or file.get("created_by") != owner_id:
                    continue
                location = file.get("id")
                if isinstance(location, str) and re.fullmatch(r"[0-9a-f]{32}", location):
                    locations[location] = None
        return list(locations)

    @staticmethod
    async def _has_other_reference(db: AsyncSession, session_id: str, location: str) -> bool:
        # Look at identifiers only, including nested Agent file inputs. Do not
        # infer namespace authority from another session's client-written JSON.
        path = cast("$.** ? (@.id == $attachment || @.file_id == $attachment)", JSONPATH)
        variables = cast({"attachment": location}, JSONB)
        if await db.scalar(select(Conversation.id).where(Conversation.id != session_id, func.jsonb_path_exists(Conversation.message, path, variables)).limit(1)):
            return True
        return (
            await db.scalar(
                select(API4Conversation.id).where(or_(func.jsonb_path_exists(API4Conversation.message, path, variables), func.jsonb_path_exists(API4Conversation.dsl, path, variables))).limit(1)
            )
            is not None
        )

    @classmethod
    async def delete_session(cls, db: AsyncSession, *, tenant_id: str, chat_id: str, session_id: str, owner_id: str) -> bool:
        """Commit SQL deletion only after confirmed cleanup of this owner's uploads."""
        try:
            # Serialize concurrent deletions in the same upload namespace, so
            # the final session sees earlier committed removals of shared refs.
            await db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"runtime-upload-cleanup:{owner_id}", 0))))
            conversation = await db.scalar(
                select(Conversation)
                .join(Dialog, Dialog.id == Conversation.dialog_id)
                .where(Conversation.id == session_id, Conversation.dialog_id == chat_id, Dialog.tenant_id == tenant_id)
                .with_for_update(of=Conversation)
                .execution_options(populate_existing=True)
            )
            if conversation is None:
                await db.rollback()
                return False
            locations = []
            for location in cls.upload_locations(conversation.message, owner_id):
                if not await cls._has_other_reference(db, session_id, location):
                    locations.append(location)
            cleanup = asyncio.create_task(asyncio.to_thread(FileService.remove_runtime_uploads, owner_id, locations))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # A cancelled HTTP request must not leave a deletion thread
                # running after its transaction/namespace lock is released.
                await FileService._drain_runtime_task(cleanup)
                raise
            await db.delete(conversation)
            await db.commit()
            return True
        except BaseException:
            await db.rollback()
            raise
