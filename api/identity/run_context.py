"""Immutable, process-local identity context for one MultiRAG execution run."""

from __future__ import annotations

from dataclasses import dataclass, field

from api.identity.principal import Principal


@dataclass(frozen=True, slots=True)
class RunContext:
    """Trusted identity facts propagated without entering model-visible data."""

    tenant_id: str = field(repr=False)
    principal: Principal | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.tenant_id or (self.principal is not None and self.principal.tenant_id != self.tenant_id):
            raise ValueError("run identity context is inconsistent")

    @property
    def platform_user_id(self) -> str | None:
        if self.principal is None:
            return None
        return self.principal.platform_user_id


__all__ = ["RunContext"]
