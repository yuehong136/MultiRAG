"""Read-only compatibility checks for switching a dataset's embedding model."""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any, Protocol, runtime_checkable

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import Knowledgebase
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from common import settings
from common.constants import LLMType, RetCode
from common.doc_store.doc_store_base import OrderByExpr
from core.nlp import search


@runtime_checkable
class EmbeddingModel(Protocol):
    def encode(self, texts: list[str]) -> tuple[Any, int]: ...


def _vector(value: Any) -> np.ndarray:
    if isinstance(value, str):
        value = value.split("\t")
    vector = np.asarray(value, dtype=np.float64)
    if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all() or not np.isfinite(np.linalg.norm(vector)) or np.linalg.norm(vector) <= 1e-12:
        raise ValueError("Embedding vectors must be non-empty, finite, one-dimensional and non-zero.")
    return vector


def _cosine(left: np.ndarray, right: np.ndarray) -> float:
    if left.shape != right.shape:
        raise ValueError(f"Embedding dimension {left.size} differs from stored dimension {right.size}.")
    score = float(np.dot(left / np.linalg.norm(left), right / np.linalg.norm(right)))
    if not np.isfinite(score):
        raise ValueError("Embedding similarity must be finite.")
    return float(np.clip(score, -1, 1))


def _compare(tenant_id: str, dataset_id: str, dataset_name: str, model: EmbeddingModel, embd_id: str, count: int) -> tuple[bool, Any, RetCode]:
    store = settings.docStoreConn
    # The production backend uses one collection per dataset, keyed by its current name.
    index = search.index_name_one(tenant_id, dataset_name)
    if not store.index_exist(index, dataset_id):
        return False, "No embedded chunks are available to compare.", RetCode.DATA_ERROR
    args = {
        "select_fields": ["id"],
        "highlight_fields": [],
        "condition": {"kb_id": dataset_id, "available_int": 1},
        "match_expressions": [],
        "order_by": OrderByExpr(),
        "index_names": [index],
        "dataset_ids": [dataset_id],
    }
    first = store.search(**args, offset=0, limit=1)
    total = store.get_total(first)
    if total <= 0:
        return False, "No embedded chunks are available to compare.", RetCode.DATA_ERROR
    offsets = sorted(random.sample(range(min(total, 1000)), min(count, total, 1000)))
    results: list[dict[str, Any]] = []
    scores: list[float] = []
    modes: set[str] = set()
    for offset in offsets:
        found = first if offset == 0 else store.search(**args, offset=offset, limit=1)
        ids = store.get_doc_ids(found)
        if not ids:
            continue
        chunk = store.get(ids[0], index, [dataset_id]) or {}
        # A direct get must still belong to the requested dataset.
        kb_ids = chunk.get("kb_id")
        if kb_ids != dataset_id and kb_ids != [dataset_id]:
            raise ValueError("Sampled chunk does not belong to the dataset.")
        vector_fields = [key for key in chunk if re.fullmatch(r"q_\d+_vec", key)]
        if len(vector_fields) != 1:
            raise ValueError("Sampled chunk must contain exactly one embedding vector.")
        field = vector_fields[0]
        stored = _vector(chunk[field])
        if int(field.split("_")[1]) != stored.size:
            raise ValueError("Stored vector dimension disagrees with its field.")
        questions = chunk.get("question_kwd") or []
        text = "\n".join(questions) if isinstance(questions, list) else questions
        text = text or chunk.get("content_with_weight") or ""
        text = re.sub(r"</?(table|td|caption|tr|th)( [^<>]{0,12})?>", " ", text).strip()
        if not text:
            results.append({"chunk_id": ids[0], "reason": "no_text"})
            continue
        vectors, _ = model.encode([chunk.get("docnm_kwd") or "Title", text])
        if len(vectors) != 2:
            raise ValueError("Embedding provider must return one vector per input.")
        title, content = (_vector(value) for value in vectors)
        _cosine(title, stored)  # Validate title dimensions before NumPy broadcasting.
        content_score = _cosine(content, stored)
        mixed = 0.1 * title + 0.9 * content
        mixed_score = _cosine(mixed, stored) if np.linalg.norm(mixed) > 1e-12 else 0.0
        score = max(content_score, mixed_score)
        mode = "title+content" if mixed_score > content_score else "content_only"
        modes.add(mode)
        scores.append(score)
        results.append({"chunk_id": ids[0], "doc_id": chunk.get("doc_id"), "doc_name": chunk.get("docnm_kwd"), "vector_field": field, "vector_dim": stored.size, "cos_sim": round(score, 6)})
    if not scores:
        return False, "No embedded chunks are available to compare.", RetCode.DATA_ERROR
    average = float(np.mean(scores))
    data = {
        "summary": {
            "kb_id": dataset_id,
            "model": embd_id,
            "sampled": len(results),
            "valid": len(scores),
            "avg_cos_sim": round(average, 6),
            "min_cos_sim": round(min(scores), 6),
            "max_cos_sim": round(max(scores), 6),
            "match_mode": next(iter(modes)) if len(modes) == 1 else "mixed",
        },
        "results": results,
    }
    if average >= 0.9:
        return True, data, RetCode.SUCCESS
    return False, data, RetCode.NOT_EFFECTIVE


async def check_embedding(db: AsyncSession, tenant_id: str, dataset_id: str, embd_id: str, check_num: int = 5) -> tuple[bool, Any, RetCode]:
    if not await KnowledgebaseService.accessible_async(db, dataset_id, tenant_id):
        return False, "No authorization.", RetCode.AUTHENTICATION_ERROR
    kb = await db.scalar(select(Knowledgebase).where(Knowledgebase.id == dataset_id))
    if kb is None:
        return False, "Invalid Dataset ID", RetCode.DATA_ERROR
    owner, dataset_name = kb.tenant_id, kb.name

    def build(sync_db: Session) -> LLMBundle:
        config = get_model_config_by_type_and_name(sync_db, owner, LLMType.EMBEDDING.value, embd_id)
        if config.get("status") == "0":
            raise LookupError("Embedding model is disabled")
        bundle = LLMBundle(sync_db, owner, config)
        bundle.db = None
        return bundle

    try:
        bundle = await db.run_sync(build)  # TODO(async-phase4): legacy model construction
    except (LookupError, ValueError):
        return False, "Embedding model is not available for this dataset's tenant.", RetCode.DATA_ERROR
    # Release the read transaction before provider and storage IO.
    await db.rollback()
    try:
        return await asyncio.to_thread(_compare, owner, dataset_id, dataset_name, bundle, embd_id, check_num)
    except (ValueError, TypeError, OverflowError):
        return False, "Embedding comparison failed: invalid vectors or sample data.", RetCode.DATA_ERROR
