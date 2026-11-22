"""Owned published entries and explicit human judgments for read-only startup tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any, TypedDict

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Base, Memory, Task, TenantLLM
from api.db.memory_verification_models import MemoryVerificationHead, MemoryVerificationOperation, MemoryVerificationRecord, MemoryVerificationSource
from tests.support.memory_writes_http import api_request, result


class StartupScope(TypedDict):
    user_id: str
    agent_id: str
    session_id: str | None


SCOPE: StartupScope = {"user_id": "existing-subject", "agent_id": "existing-agent", "session_id": None}
APPLICABILITY = "Only the project port in development. The backup policy was not checked. 仅开发环境，不用于生产审批。"
_TABLES = (MemoryVerificationSource, MemoryVerificationOperation, MemoryVerificationHead, MemoryVerificationRecord)


def endpoint(env: dict[str, Any], suffix: str = "") -> str:
    return f"/memories/{env['memory_id']}" + suffix


def confirm(env: dict[str, Any], *, entry_id: str | None = None, operation: str = "human-check-for-startup", decision: str = "human_confirmed") -> dict[str, Any]:
    identity = entry_id if entry_id is not None else env["entry_id"]
    prefix = endpoint(env, "/entries/" + identity)
    observed = result(api_request(env, "GET", prefix + "/verification-snapshot"))
    return result(
        api_request(
            env,
            "POST",
            prefix + "/verifications",
            json={
                "operation_id": operation,
                "expected_head_version": observed["head_version"],
                "expected_binding_sha256": observed["binding_sha256"],
                "result": decision,
                "method": "source_comparison",
                "applicability": APPLICABILITY,
                "notes": "Synthetic human judgment, not an automatic fact check.",
                "evidence": [{"description": "The version-bound source is comparison context, not proof of every external fact.", "reference": "source:900"}],
            },
        )
    )


@pytest.fixture
def startup_api(dreaming_api: dict[str, Any]) -> Iterator[dict[str, Any]]:
    env = dreaming_api
    env["source_texts"] = {"900": "Owned raw personal"}
    env["journal"].event("reserve-startup-verification-scope", database=env["engine"].url.database, memory_id=env["memory_id"], tables=[model.__tablename__ for model in _TABLES])
    try:
        env["startup_verification"] = confirm(env)
        yield env
    finally:
        with Session(env["engine"]) as db:
            for model in _TABLES:
                db.execute(sa.delete(model).where(model.memory_id == env["memory_id"]))
            db.commit()
        with Session(env["engine"]) as reader:
            remaining = {model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(model.memory_id == env["memory_id"])) for model in _TABLES}
            assert not any(remaining.values()), remaining
        env["journal"].event("startup-verification-cleanup-readback", database=env["engine"].url.database, remaining=remaining)


def sql_fingerprint(env: dict[str, Any]) -> str:
    """Independent exact-bank readback includes metadata, tasks and model counters."""
    state: dict[str, Any] = {}
    with Session(env["engine"]) as reader:
        for table in Base.metadata.sorted_tables:
            if table.schema == "usr_ai" and "memory_id" in table.c:
                rows = reader.execute(sa.select(table).where(table.c.memory_id == env["memory_id"])).mappings().all()
                state[table.fullname] = sorted(json.dumps(dict(row), sort_keys=True, default=str) for row in rows)
        for model, column, identity in ((Memory, Memory.id, env["memory_id"]), (Task, Task.doc_id, env["memory_id"]), (TenantLLM, TenantLLM.tenant_id, env["tenants"]["personal"])):
            rows = reader.execute(sa.select(model.__table__).where(column == identity)).mappings().all()
            state[model.__tablename__] = sorted(json.dumps(dict(row), sort_keys=True, default=str) for row in rows)
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
