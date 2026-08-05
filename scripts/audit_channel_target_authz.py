"""Read-only audit: which enabled channel bindings the target check would refuse.

Target authorization (CHN-S5) is deliberately *not* re-run for already-running
channels -- an API deploy must never take a live production channel down, and a
runtime that silently stopped reconciling would give the admin no signal at
all. So a binding created before the check existed keeps working until someone
next writes to it (enable / PUT binding / a PATCH carrying a binding), at which
point it is refused.

This script finds those bindings ahead of time, so an operator can transfer
ownership or grant a role before anyone walks into that wall.

Usage::

    uv run --no-sync python scripts/audit_channel_target_authz.py
    uv run --no-sync python scripts/audit_channel_target_authz.py --strict

It only reads. It never writes, and never prints a credential.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.channel_control.repository import ChannelRepository, SqlAlchemyChannelRepository
from api.db.db_models import ChannelBinding, ChatChannel, async_session_factory
from common.constants import TenantPermission

_NO_TARGET = "target no longer exists"
_PRIVATE_TARGET = "target belongs to another tenant and is not shared with the team"
_NO_ROLE = "caller lacks an updater role in the tenant that owns the target"


@dataclass(frozen=True, slots=True)
class Finding:
    """One enabled binding that the current target check would refuse."""

    channel_id: str
    channel_name: str
    tenant_id: str
    target_type: str
    target_id: str
    owner_tenant_id: str | None
    reason: str


async def _check_binding(
    repository: ChannelRepository,
    channel: ChatChannel,
    binding: ChannelBinding,
) -> Finding | None:
    """Mirror of ChannelControlService._validate_target, minus the revision check.

    Revision staleness is surfaced separately (``binding.revision_stale`` on the
    read path) and is not an authorization problem, so it is out of scope here.
    """

    owner: str | None
    if binding.target_type == "multirag.dialog":
        owner = await repository.resolve_dialog_owner(binding.target_id)
        if owner is None:
            reason = _NO_TARGET
        elif owner == channel.tenant_id or await repository.user_can_update_tenant_resources(channel.tenant_id, owner):
            return None
        else:
            reason = _NO_ROLE
    else:
        resolved = await repository.resolve_canvas_owner(binding.target_id)
        if resolved is None:
            owner, reason = None, _NO_TARGET
        else:
            owner, permission = resolved
            if owner == channel.tenant_id:
                return None
            if permission != TenantPermission.TEAM:
                reason = _PRIVATE_TARGET
            elif await repository.user_can_update_tenant_resources(channel.tenant_id, owner):
                return None
            else:
                reason = _NO_ROLE

    return Finding(
        channel_id=channel.id,
        channel_name=channel.name,
        tenant_id=channel.tenant_id,
        target_type=binding.target_type,
        target_id=binding.target_id,
        owner_tenant_id=owner,
        reason=reason,
    )


async def audit() -> list[Finding]:
    """Every enabled binding that would now be refused, in discovery order."""

    if async_session_factory is None:
        raise RuntimeError("No async session factory configured; the async engine requires a PostgreSQL backend.")

    findings: list[Finding] = []
    async with async_session_factory() as session:
        repository = SqlAlchemyChannelRepository(session)
        for channel, binding, _secret in await repository.list_runtime_bindings():
            finding = await _check_binding(repository, channel, binding)
            if finding is not None:
                findings.append(finding)
    return findings


def _render(findings: list[Finding]) -> str:
    if not findings:
        return "audit_channel_target_authz: OK -- every enabled binding passes the target check."

    lines = [
        f"audit_channel_target_authz: {len(findings)} enabled binding(s) would be refused on the next write.",
        "",
        f"{'CHANNEL':<34}{'TENANT':<34}{'TARGET':<34}{'OWNER':<34}REASON",
    ]
    for finding in findings:
        lines.append(f"{finding.channel_id:<34}{finding.tenant_id:<34}{finding.target_id:<34}{finding.owner_tenant_id or '-':<34}{finding.reason}")
    lines += [
        "",
        "These channels keep running until someone next writes to them. Fix by either",
        "sharing the target with the team, granting the channel owner an owner/admin role",
        "in the tenant that owns the target, or re-pointing the binding at an owned target.",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when any binding would be refused (for use in a deployment pipeline)",
    )
    args = parser.parse_args()

    findings = asyncio.run(audit())
    print(_render(findings))
    return 1 if args.strict and findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
