"""Immutable, process-local identity context for one MultiRAG execution run."""

from __future__ import annotations

from dataclasses import dataclass, field

from api.identity.principal import Principal


@dataclass(frozen=True, slots=True)
class DraftExecutionTarget:
    """Server-selected development snapshot, separate from a published revision."""

    agent_id: str
    snapshot_digest: str

    def __post_init__(self) -> None:
        if not self.agent_id.strip() or len(self.snapshot_digest) != 64 or any(char not in "0123456789abcdef" for char in self.snapshot_digest):
            raise ValueError("draft execution target is invalid")


@dataclass(frozen=True, slots=True)
class RunContext:
    """Trusted identity facts propagated without entering model-visible data."""

    tenant_id: str = field(repr=False)
    principal: Principal | None = field(default=None, repr=False)
    agent_id: str | None = field(default=None, repr=False)
    agent_revision_id: str | None = field(default=None, repr=False)

    draft_target: DraftExecutionTarget | None = field(default=None, repr=False)
    mcp_read_only: bool = False

    def __post_init__(self) -> None:
        if self.draft_target is not None and self.agent_id is not None:
            raise ValueError("draft and published execution targets cannot be combined")
        if not self.tenant_id or (self.principal is not None and self.principal.tenant_id != self.tenant_id):
            raise ValueError("run identity context is inconsistent")
        if (self.agent_id is None) != (self.agent_revision_id is None):
            raise ValueError("run execution target requires both agent and published revision")
        if self.agent_id is not None and (not self.agent_id.strip() or not self.agent_revision_id or not self.agent_revision_id.strip()):
            raise ValueError("run execution target is invalid")

    @property
    def platform_user_id(self) -> str | None:
        if self.principal is None:
            return None
        return self.principal.platform_user_id

    @property
    def identity_revision(self) -> int | None:
        if self.principal is None:
            return None
        return self.principal.identity_revision


__all__ = ["DraftExecutionTarget", "RunContext"]
