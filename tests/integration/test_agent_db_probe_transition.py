"""Registered probe route: real login, refusal side effects and scratch SQL."""

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests
import sqlalchemy as sa

from tests.support.agent_update_release import release_api as release_api
from tests.support.database import scratch_database


@pytest.fixture
def bootstrapped_engine(postgres_service: None, tmp_path: Path) -> Iterator[sa.Engine]:
    evidence = Path(os.environ.get("MULTIRAG_G_EVIDENCE_DIR", str(tmp_path)))
    with scratch_database(evidence) as engine:
        yield engine


def snapshot(engine: sa.Engine) -> str:
    from api.db.db_models import APIToken, Tenant, User, UserTenant

    with engine.connect() as connection:
        rows = [[dict(row._mapping) for row in connection.execute(sa.select(model.__table__).order_by(*model.__table__.primary_key))] for model in [APIToken, Tenant, User, UserTenant]]
    return hashlib.sha256(json.dumps(rows, default=str, sort_keys=True).encode()).hexdigest()


def test_registered_probe_auth_and_wrapped_private_denials(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from api.apps.services import agent_db_probe
    from common import ssrf_guard

    env = release_api
    endpoint = env["base"] + "/api/v1/agents/test_db_connection"
    payload = {"db_type": "postgres", "database": "unused", "username": "user", "host": "db.example", "port": 5432, "password": "never-disclose-secret"}
    policy_calls: list[str] = []
    driver_calls: list[str] = []
    writes: list[str] = []
    original_engine = agent_db_probe.create_engine
    original = ssrf_guard.assert_host_is_safe

    def policy(host: str) -> str:
        policy_calls.append(host)
        return original(host)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        driver_calls.append("driver")
        pytest.fail("rejected host reached a driver")

    def observe_write(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if statement.lstrip().split(maxsplit=1)[0].lower() in {"insert", "update", "delete"}:
            writes.append(statement)

    monkeypatch.setattr(agent_db_probe, "assert_host_is_safe", policy)
    monkeypatch.setattr(agent_db_probe, "create_engine", forbidden)
    monkeypatch.setattr(agent_db_probe, "import_module", forbidden)
    sa.event.listen(sa.engine.Engine, "before_cursor_execute", observe_write)
    before = snapshot(env["engine"])
    try:
        response = requests.post(endpoint, json=payload, timeout=30)
        assert response.status_code == 401 and policy_calls == []
        statuses = []
        # manager is the existing login dependency, not an admin role check.
        # Both real user JWTs reach policy validation and remain denied by SSRF.
        for host in ["64:ff9b::7f00:1", "::127.0.0.1"]:
            for jwt in env["jwts"][:2]:
                response = requests.post(endpoint, headers={"Authorization": "Bearer " + jwt}, json=dict(payload, host=host), timeout=30)
                body = response.json()
                assert response.status_code == 200 and body["retcode"] != 0 and "public" in body["retmsg"]
                assert "never-disclose-secret" not in response.text
                statuses.append({"host": host, "status": response.status_code, "retcode": body["retcode"]})
        assert len(policy_calls) == 4 and not driver_calls and not writes
        assert snapshot(env["engine"]) == before and env["task_ids"] == []
        target = env["engine"].url
        real = {"db_type": "postgres", "database": target.database, "username": target.username, "host": "db.example", "port": target.port or 5432, "password": target.password}
        # Admit only this fixture-owned endpoint for the positive driver test.
        with monkeypatch.context() as controlled:
            controlled.setattr(agent_db_probe, "create_engine", original_engine)
            controlled.setattr(agent_db_probe, "assert_host_is_safe", lambda _: target.host)
            for _ in range(3):
                response = env["client"].post(endpoint, json=real, timeout=30)
                assert response.status_code == 200 and response.json()["retcode"] == 0
            response = env["client"].post(endpoint, json=dict(real, database="missing_" + target.database), timeout=30)
            assert response.status_code == 200 and response.json()["retcode"] != 0
        with env["engine"].connect() as connection:
            remaining = connection.scalar(sa.text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND application_name=''"))
            assert remaining == 0
        assert not writes and snapshot(env["engine"]) == before
    finally:
        sa.event.remove(sa.engine.Engine, "before_cursor_execute", observe_write)
    evidence = Path(os.environ.get("MULTIRAG_G_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "probe-denials.json").write_text(
        json.dumps(
            {
                "database": env["engine"].url.database,
                "statuses": statuses,
                "policyCalls": len(policy_calls),
                "driverCalls": driver_calls,
                "sqlWrites": len(writes),
                "beforeSHA256": before,
                "afterSHA256": snapshot(env["engine"]),
                "postgresConnectionsRemaining": remaining,
            },
            indent=2,
        )
    )
