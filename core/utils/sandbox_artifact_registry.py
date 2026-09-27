"""Short-lived ownership records for sandbox objects emitted by CodeExec."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from common.constants import SANDBOX_ARTIFACT_EXPIRE_DAYS
from core.utils.redis_conn import REDIS_CONN

_FILENAME = re.compile(r"[0-9a-f]{32}\.(?:png|jpg|jpeg|svg|pdf|csv|json|html)\Z")
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_KEY_PREFIX = "multirag:sandbox_artifact:"


@dataclass(frozen=True)
class ArtifactBinding:
    owner_id: str
    run_id: str
    session_id: str | None


def record_artifact_binding(filename: str, owner_id: str, run_id: str, session_id: str | None) -> bool:
    """Record the server-created object and its exact execution before exposing its URL."""
    if not _FILENAME.fullmatch(filename) or not owner_id or not _RUN_ID.fullmatch(run_id):
        return False
    if session_id is not None and not _RUN_ID.fullmatch(session_id):
        return False
    binding = {"owner_id": owner_id, "run_id": run_id, "session_id": session_id}
    return bool(REDIS_CONN.set_obj(f"{_KEY_PREFIX}{filename}", binding, exp=max(1, SANDBOX_ARTIFACT_EXPIRE_DAYS * 86400)))


def get_artifact_binding(filename: str) -> ArtifactBinding | None:
    if not _FILENAME.fullmatch(filename):
        return None
    raw = REDIS_CONN.get(f"{_KEY_PREFIX}{filename}")
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
        owner_id = value["owner_id"]
        run_id = value["run_id"]
        session_id = value["session_id"]
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(owner_id, str) or not owner_id or not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        return None
    if session_id is not None and (not isinstance(session_id, str) or not _RUN_ID.fullmatch(session_id)):
        return None
    return ArtifactBinding(owner_id=owner_id, run_id=run_id, session_id=session_id)
