"""Bind a model by its tenant row ID without holding SQL across provider IO."""

import asyncio
import math
from typing import Any
from urllib.parse import urljoin

import httpx
from sqlalchemy import select

from api.db.db_models import TenantLLM, async_db_connection
from api.skills.search_types import SkillSearchError

STRICT_RERANK_PROVIDERS = frozenset({"OpenAI-API-Compatible", "VLLM"})


def rerank_scores(payload: Any, count: int) -> list[float]:
    """Missing/duplicate candidates are provider failures, never zero scores."""
    try:
        results = payload["results"]
        if not isinstance(results, list) or len(results) != count:
            raise ValueError("Incomplete results")
        scores: dict[int, float] = {}
        for item in results:
            index = item["index"]
            score = item["relevance_score"]
            if type(index) is not int or not 0 <= index < count or index in scores or type(score) not in (int, float) or not math.isfinite(score):
                raise ValueError("Invalid result")
            scores[index] = float(score)
        return [scores[index] for index in range(count)]
    except (KeyError, TypeError, ValueError, OverflowError):
        raise SkillSearchError("RERANK_INVALID", "Reranker returned incomplete or invalid scores") from None


class ModelRuntime:
    async def models(self, tenant_id: str) -> list[dict[str, Any]]:
        from core.llm import EmbeddingModel

        async with async_db_connection() as db:
            rows = (await db.scalars(select(TenantLLM).where(TenantLLM.tenant_id == tenant_id, TenantLLM.status == "1", TenantLLM.mdl_type.in_(["embedding", "rerank"])).order_by(TenantLLM.id))).all()
            return [
                {
                    "id": str(row.id),
                    "name": row.llm_name,
                    "provider": row.llm_factory,
                    "type": row.mdl_type,
                    "max_tokens": row.max_tokens,
                    "available": row.llm_factory in (EmbeddingModel if row.mdl_type == "embedding" else STRICT_RERANK_PROVIDERS),
                    "reason": None if row.llm_factory in (EmbeddingModel if row.mdl_type == "embedding" else STRICT_RERANK_PROVIDERS) else "MODEL_DRIVER_UNAVAILABLE",
                }
                for row in rows
            ]

    async def resolve(self, tenant_id: str, model_id: Any, model_type: str) -> dict[str, Any]:
        if not isinstance(model_id, str) or not model_id.isascii() or not model_id.isdecimal() or not 0 < int(model_id) < 2**63:
            raise SkillSearchError("MODEL_NOT_FOUND", "An enabled model ID is required", False)
        async with async_db_connection() as db:
            row = await db.scalar(select(TenantLLM).where(TenantLLM.id == int(model_id), TenantLLM.tenant_id == tenant_id, TenantLLM.mdl_type == model_type, TenantLLM.status == "1"))
            if row is None:
                raise SkillSearchError("MODEL_NOT_FOUND", "Model is unavailable for this tenant", False)
            config = {key: getattr(row, key) for key in ("id", "llm_name", "llm_factory", "mdl_type", "api_key", "api_base", "max_tokens")}
        from core.llm import EmbeddingModel

        if config["llm_factory"] not in (EmbeddingModel if model_type == "embedding" else STRICT_RERANK_PROVIDERS):
            raise SkillSearchError("MODEL_DRIVER_UNAVAILABLE", "Model driver is unavailable", False)
        return config

    async def encode(self, tenant_id: str, config: dict[str, Any], texts: list[str], *, query: bool = False) -> Any:
        def invoke() -> Any:
            from api.db.services.llm_service import LLMBundle

            model = LLMBundle(None, tenant_id, config)
            result, _ = model.encode_queries(texts[0]) if query else model.encode(texts)
            return [result] if query else result

        try:
            return await asyncio.to_thread(invoke)
        except Exception:
            raise SkillSearchError("EMBEDDING_FAILED", "Embedding provider failed") from None

    async def rerank(self, tenant_id: str, config: dict[str, Any], query: str, texts: list[str]) -> Any:
        try:
            base = config["api_base"] or ""
            endpoint = base if "/rerank" in base else urljoin(base, "/rerank")
            async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                response = await client.post(
                    endpoint,
                    headers={"Authorization": "Bearer " + (config["api_key"] or "")},
                    json={"model": config["llm_name"].split("___")[0], "query": query, "documents": texts, "top_n": len(texts)},
                )
                response.raise_for_status()
                scores = rerank_scores(response.json(), len(texts))
            return scores
        except SkillSearchError:
            raise
        except Exception:
            raise SkillSearchError("RERANK_FAILED", "Rerank provider failed") from None
