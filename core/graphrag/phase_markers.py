"""Redis hints bound to a successfully persisted graph revision (seven day TTL)."""

import logging

from core.utils.redis_conn import REDIS_CONN

PHASE_RESOLUTION = "resolution_done"
PHASE_COMMUNITY = "community_done"
ALL_PHASES = (PHASE_RESOLUTION, PHASE_COMMUNITY)


def phase_key(kb_id: str, phase: str) -> str:
    return f"graphrag:phase:{kb_id}:{phase}"


def has_phase_marker(kb_id: str, phase: str, revision: str) -> bool:
    if not kb_id or not revision or phase not in ALL_PHASES:
        return False
    try:
        value = REDIS_CONN.get(phase_key(kb_id, phase))
        if isinstance(value, bytes):
            value = value.decode()
        return value == revision
    except Exception:
        logging.exception("GraphRAG marker read failed for %s/%s", kb_id, phase)
        return False


def set_phase_marker(kb_id: str, phase: str, revision: str, ttl: int = 604800) -> bool:
    if not kb_id or not revision or phase not in ALL_PHASES:
        return False
    try:
        return bool(REDIS_CONN.set(phase_key(kb_id, phase), revision, ttl))
    except Exception:
        logging.exception("GraphRAG marker write failed for %s/%s", kb_id, phase)
        return False


def clear_phase_markers(kb_id: str) -> None:
    if not kb_id:
        return
    for phase in ALL_PHASES:
        try:
            REDIS_CONN.delete(phase_key(kb_id, phase))
        except Exception:
            logging.exception("GraphRAG marker clear failed for %s/%s", kb_id, phase)
