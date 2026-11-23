"""Authenticated, strict-source adapters for explicit context artifact generation."""

from __future__ import annotations

import asyncio
from typing import Any

from openai import AsyncOpenAI, OpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services import memory_source_service
from api.apps.services.memory_context_service import require_memory
from api.db.joint_services.memory_write_runtime import supports_coordinated_writes
from api.db.memory_revision_models import ManagedMemoryState
from api.db.services.llm_service import LLMBundle, llm_bundle_scope
from common.asyncio_utils import finish_owned_work
from common.constants import LLMType
from common.token_utils import encoder
from memory.compaction.domain import SYSTEM
from memory.compaction.service import CompactionService
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionValidationError, content_hash, is_active, message_scope
from memory.revisions.storage import ManagedMessageStore
from memory.writes.providers import MemoryWriteProviders, release_sql


async def compactions(db: AsyncSession, memory_id: str, *, actor_id: str) -> CompactionService:
    binding = await require_memory(db, memory_id, actor_id=actor_id)
    if not supports_coordinated_writes():
        raise RevisionValidationError("compaction_backend_unsupported")
    provider = MemoryWriteProviders(db, tenant_id=binding.tenant_id, actor_id=actor_id)
    storage = ManagedMessageStore(binding.tenant_id, memory_id)

    async def load_sources(mid: str, source_ids: list[str]) -> list[dict[str, Any]]:
        if mid != memory_id:
            raise RevisionNotFound("memory_not_found")
        await provider.authorize(db, mid)
        bank = await db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == mid, ManagedMemoryState.tenant_id == provider.tenant_id))
        if bank is None or bank.state != "active":
            await release_sql(db)
            raise RevisionConflict("compaction_managed_memory_required")
        await release_sql(db)
        roots = []
        for source_id in source_ids:
            metadata = await memory_source_service.get_provenance(db, mid, int(source_id), actor_id=actor_id, include_segments=True)
            if metadata["source_revision"] is None or metadata["source_generation"] is None:
                raise RevisionConflict("compaction_source_registration_required")
            await release_sql(db)
            physical = await storage.read(int(source_id))
            if (
                physical is None
                or physical.get("message_type") != "raw"
                or physical.get("source_id") not in (None, 0, "0", "")
                or isinstance(physical.get("source_id"), bool)
                or not is_active(physical)
                or not isinstance(physical.get("content"), str)
            ):
                raise RevisionNotFound("compaction_source_unavailable")
            if content_hash(physical["content"]) != metadata["content_sha256"] or message_scope(physical, mid) != metadata["scope"] or physical.get("valid_at") != metadata["reference_time"]:
                raise RevisionConflict("compaction_source_changed")
            root = {
                key: metadata[key]
                for key in ("source_id", "source_revision", "source_generation", "content_sha256", "scope", "provenance_sha256", "format", "role_status", "reference_time", "event_time")
            }
            root["source_id"] = str(root["source_id"])
            root["locator"] = {"kind": "memory_message", "memory_id": mid, "message_id": source_id}
            roots.append({"binding": root, "content": physical["content"], "declared_roles": metadata["declared_roles"]})
        return roots

    async def model_identity(mid: str) -> dict[str, Any]:
        settings, prepared = await provider._prepared(mid, LLMType.CHAT.value)
        await release_sql(db)
        identifier = prepared.model_config.get("id")
        return {
            "model_reference": f"{prepared.model_config['llm_name']}@{prepared.model_config['llm_factory']}",
            "config_reference": provider._chat_fingerprint(settings, prepared),
            "tenant_model_id": identifier,
        }

    async def summarize(mid: str, body: str, output_budget_tokens: int, expected: dict[str, Any]) -> str:
        settings, prepared = await provider._prepared(mid, LLMType.CHAT.value)
        await release_sql(db)
        if provider._chat_fingerprint(settings, prepared) != expected["config_reference"]:
            raise RevisionConflict("compaction_model_changed")
        async with llm_bundle_scope():
            bundle = await finish_owned_work(asyncio.to_thread(LLMBundle.from_prepared, prepared, max_retries=0))
            # These clients belong to this scoped bundle. Keep their identities
            # (including SDK observers) while disabling SDK-level paid retries.
            for attribute in ("client", "async_client"):
                client = getattr(bundle.mdl, attribute, None)
                if isinstance(client, (OpenAI, AsyncOpenAI)):
                    client.max_retries = 0
            generation: dict[str, Any] = {"temperature": 0, "max_tokens": output_budget_tokens}
            options: dict[str, Any] = {}
            cleaner = getattr(getattr(bundle, "mdl", None), "_clean_conf", None)
            if callable(cleaner):
                cleaned = cleaner(dict(generation))
                if isinstance(cleaned, dict) and not {"max_tokens", "max_completion_tokens", "max_output_tokens", "max_new_tokens"}.intersection(cleaned):
                    options["max_tokens"] = output_budget_tokens
            response = await finish_owned_work(bundle.async_chat(SYSTEM, [{"role": "user", "content": body}], generation, **options))
        if await model_identity(mid) != expected:
            raise RevisionConflict("compaction_model_changed")
        return response

    return CompactionService(
        db,
        tenant_id=provider.tenant_id,
        actor_id=actor_id,
        authorize=provider.authorize,
        load_sources=load_sources,
        model_identity=model_identity,
        summarize=summarize,
        count_tokens=lambda text: len(encoder.encode(text, disallowed_special=())),
    )
