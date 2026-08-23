"""Immutable, process-local identity context for one MultiRAG execution run."""

from __future__ import annotations

from dataclasses import dataclass, field

from api.identity.principal import Principal


@dataclass(frozen=True, slots=True)
class RunContext:
    """Trusted identity facts propagated without entering model-visible data."""

    tenant_id: str = field(repr=False)
    principal: Principal | None = field(default=None, repr=False)
    agent_id: str | None = field(default=None, repr=False)
    agent_revision_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
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


__all__ = ["RunContext"]
