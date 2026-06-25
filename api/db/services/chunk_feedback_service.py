#
#  Copyright 2025 The MultiRAG Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""Adjust chunk recall weights from user message feedback."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Iterable

from api.db.db_models import db_connection
from api.db.services.knowledgebase_service import KnowledgebaseService
from common import settings
from common.constants import PAGERANK_FLD
from core.nlp.search import index_name, index_name_one

CHUNK_FEEDBACK_ENABLED = os.getenv("CHUNK_FEEDBACK_ENABLED", "false").lower() == "true"
CHUNK_FEEDBACK_WEIGHTING = os.getenv("CHUNK_FEEDBACK_WEIGHTING", "relevance").strip().lower()

UPVOTE_WEIGHT_INCREMENT = 1
DOWNVOTE_WEIGHT_DECREMENT = 1
MIN_PAGERANK_WEIGHT = 0
MAX_PAGERANK_WEIGHT = 100

_SCORE_KEYS = ("similarity", "vector_similarity", "term_similarity")


def _retrieval_signal(chunk: dict) -> float:
    best = 0.0
    for key in _SCORE_KEYS:
        raw = chunk.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > best:
            best = value
    return best


def _split_integer_budget(magnitudes: list[float], budget: int) -> list[int]:
    if not magnitudes or budget == 0:
        return [0] * len(magnitudes)

    total = sum(magnitudes)
    if total <= 0:
        base = budget // len(magnitudes)
        parts = [base] * len(magnitudes)
        for i in range(budget % len(magnitudes)):
            parts[i] += 1
        return parts

    raw = [budget * magnitude / total for magnitude in magnitudes]
    parts = [math.floor(item) for item in raw]
    remainder = budget - sum(parts)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - parts[i], reverse=True)
    for i in order[:remainder]:
        parts[i] += 1
    return [int(item) for item in parts]


def _as_index_candidates(value: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Iterable):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _safe_kb_name(kb_id: str) -> str | None:
    try:
        with db_connection() as db:
            kb = KnowledgebaseService.get_by_id(db, kb_id)
            return kb.name if kb else None
    except Exception:
        logging.debug("Failed to resolve KB name for chunk feedback: %s", kb_id, exc_info=True)
        return None


class ChunkFeedbackService:
    """Update referenced chunks so future retrieval can reflect message feedback."""

    @staticmethod
    def _feedback_rows_from_reference(reference: dict | None) -> list[tuple[str, str, dict]]:
        if not reference or not isinstance(reference, dict):
            return []

        rows = []
        for chunk in reference.get("chunks", []):
            if not isinstance(chunk, dict):
                continue
            chunk_id = chunk.get("chunk_id") or chunk.get("id")
            kb_id = chunk.get("kb_id") or chunk.get("dataset_id")
            if chunk_id and kb_id:
                rows.append((str(chunk_id), str(kb_id), chunk))
        return rows

    @staticmethod
    def _index_candidates(tenant_id: str, kb_id: str) -> list[str]:
        candidates = _as_index_candidates(index_name(tenant_id))
        if kb_name := _safe_kb_name(kb_id):
            candidates.extend(_as_index_candidates(index_name_one(tenant_id, kb_name)))
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _allocate_deltas(rows: list[tuple[str, str, dict]], is_positive: bool) -> list[tuple[str, str, int]]:
        if not rows:
            return []

        sign = 1 if is_positive else -1
        weighting = CHUNK_FEEDBACK_WEIGHTING if CHUNK_FEEDBACK_WEIGHTING in {"uniform", "relevance"} else "relevance"
        if weighting == "uniform":
            step = UPVOTE_WEIGHT_INCREMENT if is_positive else DOWNVOTE_WEIGHT_DECREMENT
            return [(chunk_id, kb_id, sign * step) for chunk_id, kb_id, _chunk in rows]

        budget = UPVOTE_WEIGHT_INCREMENT if is_positive else DOWNVOTE_WEIGHT_DECREMENT
        magnitudes = [_retrieval_signal(chunk) or 1.0 for _chunk_id, _kb_id, chunk in rows]
        parts = _split_integer_budget(magnitudes, budget)
        return [
            (chunk_id, kb_id, sign * part)
            for (chunk_id, kb_id, _chunk), part in zip(rows, parts, strict=True)
            if part != 0
        ]

    @classmethod
    def update_chunk_weight(
        cls,
        tenant_id: str,
        chunk_id: str,
        kb_id: str,
        delta: int,
        row_id: int | None = None,
    ) -> bool:
        conn = settings.docStoreConn
        adjust = getattr(conn, "adjust_chunk_pagerank_fea", None)

        for idx_name in cls._index_candidates(tenant_id, kb_id):
            try:
                if callable(adjust):
                    kwargs = {"row_id": row_id} if row_id is not None else {}
                    if adjust(
                        chunk_id,
                        idx_name,
                        kb_id,
                        delta,
                        MIN_PAGERANK_WEIGHT,
                        MAX_PAGERANK_WEIGHT,
                        **kwargs,
                    ):
                        return True
                    continue

                chunk = conn.get(chunk_id, idx_name, [kb_id])
                if not chunk:
                    continue

                current_weight = float(chunk.get(PAGERANK_FLD, 0) or 0)
                new_weight = max(MIN_PAGERANK_WEIGHT, min(MAX_PAGERANK_WEIGHT, current_weight + float(delta)))
                new_value = {PAGERANK_FLD: new_weight}
                if new_weight <= 0.0 and settings.DOC_ENGINE.lower() in {"elasticsearch", "opensearch"}:
                    new_value = {"remove": PAGERANK_FLD}
                if conn.update({"id": chunk_id}, new_value, idx_name, kb_id):
                    return True
            except Exception:
                logging.exception("Error updating chunk %s pagerank in %s", chunk_id, idx_name)

        logging.warning("Chunk %s not updated for feedback; kb_id=%s tenant_id=%s", chunk_id, kb_id, tenant_id)
        return False

    @classmethod
    def apply_feedback(cls, tenant_id: str, reference: dict, is_positive: bool) -> dict:
        if not CHUNK_FEEDBACK_ENABLED:
            logging.debug("Chunk feedback feature is disabled")
            return {"success_count": 0, "fail_count": 0, "chunk_ids": [], "disabled": True}

        rows = cls._feedback_rows_from_reference(reference)
        chunk_ids = [chunk_id for chunk_id, _kb_id, _chunk in rows]
        if not rows:
            return {"success_count": 0, "fail_count": 0, "chunk_ids": []}

        deltas = cls._allocate_deltas(rows, is_positive)
        row_by_chunk = {chunk_id: chunk.get("row_id") for chunk_id, _kb_id, chunk in rows}

        success_count = 0
        fail_count = 0
        for chunk_id, kb_id, delta in deltas:
            rid = row_by_chunk.get(chunk_id)
            rid_int = None
            if rid is not None:
                try:
                    rid_int = int(rid)
                except (TypeError, ValueError):
                    pass
            if cls.update_chunk_weight(tenant_id, chunk_id, kb_id, delta, row_id=rid_int):
                success_count += 1
            else:
                fail_count += 1

        logging.info(
            "Applied %s chunk feedback to %s/%s chunks",
            "positive" if is_positive else "negative",
            success_count,
            len(deltas),
        )
        return {"success_count": success_count, "fail_count": fail_count, "chunk_ids": chunk_ids}
