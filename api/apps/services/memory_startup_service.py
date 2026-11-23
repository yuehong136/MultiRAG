"""No-model startup data adapters; exact tokenizer, fresh ACL, strict projections."""

from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services.memory_verification_service import verifications
from common.token_utils import encoder
from memory.services.startup import StartupService


def count_startup_tokens(content: str) -> int:
    return len(encoder.encode(content, disallowed_special=()))


async def startup(db: AsyncSession, memory_id: str, *, actor_id: str) -> StartupService:
    return StartupService(await verifications(db, memory_id, actor_id=actor_id), count_tokens=count_startup_tokens)
