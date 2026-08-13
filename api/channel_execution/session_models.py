"""Opaque prepared states owned by individual MultiRAG target drivers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PreparedCanvasExecution:
    """Canvas candidate head and the public head it may replace."""

    public_session_id: str | None
    execution_session_id: str | None
    source_fingerprint: str | None
    owner_token: str
    target_id: str
    expected_user_id: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class DialogHistoryHead:
    """Original public Dialog values used by the terminal compare-and-swap."""

    messages: list[dict[str, Any]] | None
    references: list[dict[str, Any]] | None
    user_id: str | None


@dataclass(slots=True)
class DialogWorkingCopy:
    """Detached Dialog conversation mutated only by one target execution."""

    id: str
    dialog_id: str
    name: str
    message: list[dict[str, Any]]
    reference: list[dict[str, Any]]
    user_id: str | None


@dataclass(frozen=True, slots=True)
class PreparedDialogExecution:
    """Detached Dialog history plus the public head it may replace."""

    public_session_id: str
    target_id: str
    expected_head: DialogHistoryHead | None
    working_copy: DialogWorkingCopy
