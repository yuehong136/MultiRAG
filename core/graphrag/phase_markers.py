"""Redis hints bound to a successfully persisted graph revision (seven day TTL)."""

import logging

from core.graphrag.persistence import GraphRAGLease
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


def set_phase_marker(kb_id: str, phase: str, revision: str, ttl: int = 604800, *, lease: GraphRAGLease | None = None, task_id: str = "") -> bool:
    if not kb_id or not revision or phase not in ALL_PHASES:
        return False
    try:
        if lease is not None:
            return bool(
                REDIS_CONN.REDIS.eval(
                    """
                if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
                if ARGV[4] ~= '' then
                    local cancelled = redis.call('GET', KEYS[3])
                    if cancelled and cancelled ~= '' then return 0 end
                end
                redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
                return 1
                """,
                    3,
                    lease.key,
                    phase_key(kb_id, phase),
                    f"{task_id}-cancel" if task_id else "",
                    lease.lock.local.token,
                    revision,
                    ttl,
                    task_id,
                )
            )
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
