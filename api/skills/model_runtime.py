"""Bind a model by its tenant row ID without holding SQL across provider IO."""

import asyncio
import math
from typing import Any
from urllib.parse import urljoin

import httpx

from api.db.db_models import async_db_connection
from api.db.model_contract import parse_model_id
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
            from api.db.services.model_provider_service import model_projection, models_for_owner

            inventory = [model_projection(*row) for row in await models_for_owner(db, tenant_id)]
            result = []
            for item in inventory:
                if item["effective_status"] != "active":
                    continue
                for kind in item["model_types"]:
                    if kind not in {"embedding", "rerank"}:
                        continue
                    supported = item["provider_name"] in (EmbeddingModel if kind == "embedding" else STRICT_RERANK_PROVIDERS)
                    result.append(
                        {
                            "id": item["id"],
                            "name": item["name"],
                            "provider": item["provider_name"],
                            "instance": item.get("instance_name", ""),
                            "type": kind,
                            "max_tokens": item["max_tokens"],
                            "available": supported,
                            "reason": None if supported else "MODEL_DRIVER_UNAVAILABLE",
                        }
                    )
            return result

    async def resolve(self, tenant_id: str, model_id: Any, model_type: str) -> dict[str, Any]:
        identifier = parse_model_id(model_id) if isinstance(model_id, str) else None
        if identifier is None:
            raise SkillSearchError("MODEL_NOT_FOUND", "An enabled model ID is required", False)
        async with async_db_connection() as db:
            from api.db.services.model_provider_service import resolve_config

            try:
                config = await resolve_config(db, tenant_id, identifier, model_type)
            except LookupError:
                raise SkillSearchError("MODEL_NOT_FOUND", "Model is unavailable for this tenant", False) from None
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
