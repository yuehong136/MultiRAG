"""U14 worker leasing is just-in-time and renewed while MCP work runs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.identity.mcp_interactions.contracts import InteractionLease
from api.identity.mcp_interactions.crypto import InteractionPayloadCipher
from api.identity.mcp_interactions.service import (
    InteractionServiceLimits,
    PersistentInteractionService,
)
from common.mcp_interactions import (
    InteractionEffect,
    InteractionLeaseFence,
    InteractionRequest,
    InteractionResume,
)


def _lease(*, job_id: str, owner: str, attempt: int) -> InteractionLease:
    fence = InteractionLeaseFence(
        job_id=job_id,
        owner=owner,
        attempt=attempt,
    )
    request = InteractionRequest(
        tenant_id="tenant-a",
        platform_user_id="user-a",
        external_identity_id="identity-a",
        identity_revision=7,
        agent_id="agent-a",
        agent_revision_id="release-a",
        mcp_server_id="server-a",
        resource_name="leave-service",
        resource_uri="https://mcp.example/leave",
        tool_name="prepare_leave",
        original_arguments={"job": job_id},
        input_requests={"leave-form": {"method": "elicitation/create"}},
        request_state="opaque-state",
        effect=InteractionEffect.PREPARE,
        replay_mode="reusable",
        policy_revision="policy-a",
        credential_generation=1,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        interaction_id=f"interaction-{job_id}",
        previous_revision=1,
        lease_fence=fence,
    )
    return InteractionLease(
        job_id=job_id,
        owner=owner,
        attempt=attempt,
        resume=InteractionResume(
            interaction_id=f"interaction-{job_id}",
            revision=1,
            request=request,
            input_responses={"leave-form": {"action": "cancel"}},
        ),
    )


class _Repository:
    def __init__(self, leases: list[InteractionLease]) -> None:
        self.leases = leases
        self.lease_limits: list[int] = []
        self.completed: list[InteractionLease] = []
        self.renewed = asyncio.Event()
        self.renewals = 0

    async def lease_ready(
        self,
        *,
        owner: str,
        lease_seconds: int,
        limit: int,
    ) -> list[InteractionLease]:
        del owner, lease_seconds
        self.lease_limits.append(limit)
        selected = self.leases[:limit]
        del self.leases[:limit]
        return selected

    async def renew(
        self,
        *,
        lease: InteractionLease,
        lease_seconds: int,
    ) -> None:
        del lease, lease_seconds
        self.renewals += 1
        self.renewed.set()

    async def complete(self, *, lease: InteractionLease, result: object) -> None:
        del result
        self.completed.append(lease)


def _service(*, batch_size: int = 3) -> PersistentInteractionService:
    return PersistentInteractionService(
        session_factory=async_sessionmaker(),
        cipher=InteractionPayloadCipher([b"u14-unit-test-key-material-32byt"[:32]]),
        limits=InteractionServiceLimits(
            lease_seconds=1,
            batch_size=batch_size,
            max_rounds=5,
            max_payload_bytes=65_536,
            ttl_seconds=37,
        ),
    )


def _install_repository(
    monkeypatch: pytest.MonkeyPatch,
    service: PersistentInteractionService,
    repository: _Repository,
) -> None:
    @asynccontextmanager
    async def repository_context() -> AsyncIterator[_Repository]:
        yield repository

    monkeypatch.setattr(service, "_repository", repository_context)


async def test_run_once_leases_each_job_immediately_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository(
        [
            _lease(job_id="job-a", owner="worker-a", attempt=1),
            _lease(job_id="job-b", owner="worker-a", attempt=1),
        ]
    )
    service = _service()
    _install_repository(monkeypatch, service, repository)
    lease_call_counts: list[int] = []

    class _Executor:
        async def execute(self, resume: InteractionResume) -> object:
            del resume
            lease_call_counts.append(len(repository.lease_limits))
            return {"status": "ok"}

    processed = await service.run_once(owner="worker-a", executor=_Executor())

    assert processed == 2
    assert repository.lease_limits == [1, 1, 1]
    assert lease_call_counts == [1, 2]
    assert len(repository.completed) == 2


async def test_long_execution_renews_lease_before_completing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _lease(job_id="job-a", owner="worker-a", attempt=1)
    repository = _Repository([lease])
    service = _service(batch_size=1)
    _install_repository(monkeypatch, service, repository)

    class _Executor:
        async def execute(self, resume: InteractionResume) -> object:
            del resume
            await asyncio.wait_for(repository.renewed.wait(), timeout=1)
            return {"status": "ok"}

    assert await service.run_once(owner="worker-a", executor=_Executor()) == 1
    assert repository.renewals >= 1
    assert repository.completed == [lease]
    assert service.ttl_seconds == 37
