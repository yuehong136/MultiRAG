"""Opaque prepared states owned by individual MultiRAG target drivers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PreparedCanvasExecution:
    """Canvas candidate head and the public head it may replace."""

    public_session_id: str | None
    execution_session_id: str | None
    source_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class PreparedDialogExecution:
    """Dialog candidate head and the public head it may replace."""

    public_session_id: str | None
    execution_session_id: str | None
    source_fingerprint: str | None
    require_visible_answer: bool
