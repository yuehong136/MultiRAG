"""Pure data types shared by Channel session protocols and implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SessionKind = Literal["canvas", "dialog"]


@dataclass(frozen=True, slots=True)
class PreparedChannelSession:
    """A private execution head and the public head it may replace."""

    kind: SessionKind
    public_session_id: str | None
    execution_session_id: str | None
    source_fingerprint: str | None
