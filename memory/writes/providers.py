"""Prepared model callbacks for coordinated memory ingestion."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import Memory
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name
from api.db.services.llm_service import LLMBundle, llm_bundle_scope
from api.db.services.memory_service import MemoryService
from api.db.services.tenant_llm_service import PreparedTenantLLM, TenantLLMService
from api.utils.memory_utils import get_memory_type_human
from common.asyncio_utils import finish_owned_work
from common.constants import LLMType
from common.exceptions import NotFoundException
from memory.revisions.domain import ModelBinding, RevisionConflict, RevisionValidationError, is_active
from memory.utils.prompt_util import PromptAssembler
from memory.writes.types import EmbeddingResult, ExtractionResult

_MODEL_FIELDS = ("id", "tenant_id", "llm_factory", "llm_name", "mdl_type", "model_type", "api_key", "api_base", "region", "max_tokens", "instance_id")
_REFERENCE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?")


@dataclass(frozen=True, repr=False)
class MemoryWriteSettings:
    memory_id: str
    tenant_id: str
    embd_id: str
    tenant_embd_id: int | None
    llm_id: str
    tenant_llm_id: int | None
    memory_type: int
    temperature: float
    system_prompt: str
    user_prompt: str


def _fingerprint(value: dict[str, Any]) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def embedding_binding(prepared: PreparedTenantLLM, dimension: int) -> ModelBinding:
    """Match existing revision fingerprints; persist no credentials themselves."""
    config = prepared.model_config
    return ModelBinding(
        tenant_model_id=config.get("id"),
        model_reference=f"{config['llm_name']}@{config['llm_factory']}",
        dimension=dimension,
        config_reference=_fingerprint({field: config.get(field) for field in _MODEL_FIELDS}),
    )


async def release_sql(db: AsyncSession) -> None:
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("External memory work requires a session without pending changes")
    if db.in_transaction():
        await db.rollback()


def _source_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if not isinstance(value, str) or not _REFERENCE_TIME.fullmatch(value.strip()):
        raise RevisionValidationError("invalid_source_reference_time")
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise RevisionValidationError("invalid_source_reference_time") from error
    return value


def _event_time(value: Any, *, required: bool) -> str | None:
    if not required and value in (None, ""):
        return None
    timestamp = _source_time(value)
    return datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")


def parse_extractions(response: Any, allowed: set[str]) -> list[dict[str, Any]]:
    """Construct approved fields; malformed output is not an empty success."""
    if not isinstance(response, str):
        raise RevisionValidationError("invalid_extraction_response")
    text = response.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as error:
        raise RevisionValidationError("invalid_extraction_response") from error
    if not isinstance(payload, dict) or not set(payload).issubset(allowed):
        raise RevisionValidationError("invalid_extraction_types")
    messages: list[dict[str, Any]] = []
    for message_type, items in payload.items():
        if not isinstance(items, list):
            raise RevisionValidationError("invalid_extraction_items")
        for item in items:
            if len(messages) >= 100 or not isinstance(item, dict):
                raise RevisionValidationError("extraction_item_limit_exceeded")
            content = item.get("content")
            if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 65535:
                raise RevisionValidationError("invalid_extraction_content")
            messages.append(
                {
                    "message_type": message_type,
                    "content": content,
                    "valid_at": _event_time(item.get("valid_at"), required=True),
                    "invalid_at": _event_time(item.get("invalid_at"), required=False),
                }
            )
    return messages


class MemoryWriteProviders:
    """One sequential SQL session; providers receive only prepared plain data."""

    def __init__(self, db: AsyncSession, *, tenant_id: str, actor_id: str) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    async def authorize(self, db: AsyncSession, memory_id: str) -> None:
        row = (await db.execute(select(Memory.tenant_id, Memory.permissions).where(Memory.id == memory_id, Memory.tenant_id == self.tenant_id))).one_or_none()
        if row is None:
            raise NotFoundException("Memory not found.")
        if row.tenant_id == self.actor_id:
            return
        tenant_ids = await db.run_sync(lambda session: MemoryService.accessible_tenant_ids(session, self.actor_id))  # TODO(async-phase4)
        if row.permissions != "team" or self.tenant_id not in tenant_ids:
            raise NotFoundException("Memory not found.")

    async def settings(self, db: AsyncSession, memory_id: str) -> MemoryWriteSettings:
        await self.authorize(db, memory_id)
        memory = await db.scalar(select(Memory).where(Memory.id == memory_id, Memory.tenant_id == self.tenant_id).execution_options(populate_existing=True))
        if memory is None:
            raise NotFoundException("Memory not found.")
        return MemoryWriteSettings(
            memory.id,
            memory.tenant_id,
            memory.embd_id,
            memory.tenant_embd_id,
            memory.llm_id,
            memory.tenant_llm_id,
            memory.memory_type,
            memory.temperature,
            memory.system_prompt or "",
            memory.user_prompt or "",
        )

    async def _prepared(self, memory_id: str, capability: str, *, db: AsyncSession | None = None) -> tuple[MemoryWriteSettings, PreparedTenantLLM]:
        session = db if db is not None else self.db
        settings = await self.settings(session, memory_id)
        identifier = settings.tenant_embd_id if capability == LLMType.EMBEDDING.value else settings.tenant_llm_id
        reference = settings.embd_id if capability == LLMType.EMBEDDING.value else settings.llm_id

        def prepare(sync_db: Session) -> PreparedTenantLLM:
            config = (
                get_model_config_by_id(sync_db, identifier, tenant_id=self.tenant_id, model_type=capability)
                if identifier
                else get_model_config_by_type_and_name(sync_db, self.tenant_id, capability, reference)
            )
            return TenantLLMService.prepare_model_instance(
                sync_db,
                self.tenant_id,
                config,
                expected_type=capability,
                allow_unregistered_builtin=config.get("id") is None and config.get("llm_factory") == "Builtin",
            )

        prepared = await session.run_sync(prepare)  # TODO(async-phase4)
        return settings, prepared

    async def model_binding(self, db: AsyncSession, memory_id: str, dimension: int) -> ModelBinding:
        _, prepared = await self._prepared(memory_id, LLMType.EMBEDDING.value, db=db)
        return embedding_binding(prepared, dimension)

    async def embed(self, memory_id: str, messages: list[dict[str, Any]], expected: ModelBinding | None) -> EmbeddingResult:
        if not messages:
            raise RevisionValidationError("empty_embedding_batch")
        _, prepared = await self._prepared(memory_id, LLMType.EMBEDDING.value)
        await release_sql(self.db)
        if expected is not None and embedding_binding(prepared, expected.dimension) != expected:
            raise RevisionConflict("embedding_model_changed")
        async with llm_bundle_scope():
            bundle = await finish_owned_work(asyncio.to_thread(LLMBundle.from_prepared, prepared))
            values, _ = await finish_owned_work(asyncio.to_thread(bundle.encode, [message["content"] for message in messages]))
        if hasattr(values, "tolist"):
            values = values.tolist()
        if not isinstance(values, list) or len(values) != len(messages):
            raise RevisionValidationError("invalid_embedding_batch")
        vectors: list[list[float]] = []
        for vector in values:
            if hasattr(vector, "tolist"):
                vector = vector.tolist()
            if not isinstance(vector, list) or not vector or any(type(value) not in {int, float} or not math.isfinite(value) for value in vector):
                raise RevisionValidationError("invalid_embedding_vector")
            vectors.append([float(value) for value in vector])
        dimension = len(vectors[0])
        if any(len(vector) != dimension for vector in vectors):
            raise RevisionValidationError("invalid_embedding_dimension")
        return {"vectors": vectors, "model_binding": embedding_binding(prepared, dimension)}

    @staticmethod
    def _chat_fingerprint(settings: MemoryWriteSettings, prepared: PreparedTenantLLM) -> str:
        fields = (*_MODEL_FIELDS, "is_tools", "transport")
        return _fingerprint(
            {
                "model": {field: prepared.model_config.get(field) for field in fields},
                "memory_type": settings.memory_type,
                "temperature": settings.temperature,
                "system_prompt": settings.system_prompt,
                "user_prompt": settings.user_prompt,
            }
        )

    async def extract(self, memory_id: str, source: dict[str, Any]) -> ExtractionResult:
        if source.get("memory_id") != memory_id or source.get("message_type") != "raw" or not is_active(source) or not isinstance(source.get("content"), str):
            raise RevisionConflict("invalid_extraction_source")
        reference_time = _source_time(source.get("valid_at"))
        settings = await self.settings(self.db, memory_id)
        allowed = set(get_memory_type_human(settings.memory_type)) & {"semantic", "episodic", "procedural"}
        if not allowed:
            await release_sql(self.db)
            return {"items": [], "model_reference": "not_applicable", "config_reference": _fingerprint({"memory_type": settings.memory_type})}
        settings, prepared = await self._prepared(memory_id, LLMType.CHAT.value)
        await release_sql(self.db)
        fingerprint = self._chat_fingerprint(settings, prepared)
        prompts = [{"role": "user", "content": settings.user_prompt}] if settings.user_prompt else []
        prompts.append({"role": "user", "content": PromptAssembler.assemble_user_prompt(source["content"], reference_time)})
        async with llm_bundle_scope():
            bundle = await finish_owned_work(asyncio.to_thread(LLMBundle.from_prepared, prepared))
            response = await finish_owned_work(
                bundle.async_chat(settings.system_prompt or PromptAssembler.assemble_system_prompt({"memory_type": sorted(allowed)}), prompts, {"temperature": settings.temperature})
            )
        current, current_prepared = await self._prepared(memory_id, LLMType.CHAT.value)
        await release_sql(self.db)
        if self._chat_fingerprint(current, current_prepared) != fingerprint:
            raise RevisionConflict("extraction_configuration_changed")
        return {
            "items": parse_extractions(response, allowed),
            "model_reference": f"{prepared.model_config['llm_name']}@{prepared.model_config['llm_factory']}",
            "config_reference": fingerprint,
        }
