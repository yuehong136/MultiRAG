"""Bounded inventory observations: real PostgreSQL, controlled physical reads.

Elapsed observations are local measurements, not hardware or production SLAs.
"""

import json
from time import perf_counter
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from api.db.db_models import Base
from tests.support.memory_maintenance_repository import environment as environment
from tests.support.memory_startup_repository import startup
from tests.support.memory_startup_scale import seed_inventory
from tests.support.resource_journal import ResourceJournal


async def counts(env: dict[str, Any]) -> dict[str, int]:
    async with env["factory"]() as db:
        return {table.name: int(await db.scalar(sa.select(sa.func.count()).select_from(table)) or 0) for table in Base.metadata.sorted_tables if table.schema == "usr_ai"}


async def observe(env: dict[str, Any], *, inventory_limit: int, max_items: int = 16, budget_tokens: int = 32768) -> dict[str, Any]:
    statements: list[str] = []
    engine = cast(AsyncEngine, env["factory"].kw["bind"]).sync_engine

    def capture(connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        statements.append(statement.strip().split(None, 1)[0].upper())

    before = await counts(env)
    env["reads"] = 0
    sa.event.listen(engine, "before_cursor_execute", capture)
    started = perf_counter()
    try:
        async with env["factory"]() as db:
            packet = await startup(db, env).build(
                "bank", user_id=env["scope"]["user_id"], agent_id=env["scope"]["agent_id"], session_id=None, inventory_limit=inventory_limit, max_items=max_items, budget_tokens=budget_tokens
            )
    finally:
        elapsed = perf_counter() - started
        sa.event.remove(engine, "before_cursor_execute", capture)
    assert await counts(env) == before
    writes = [verb for verb in statements if verb in {"INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "CREATE", "ALTER", "DROP"}]
    assert not writes and env["reads"] == packet["selection"]["physical_reads"] <= 256
    assert packet["usage"]["estimated_tokens"] <= budget_tokens
    assert len(packet["items"]) <= max_items
    assert len(statements) <= inventory_limit * 100 + 100
    observation = {
        "inventory_limit": inventory_limit,
        "max_items": max_items,
        "budget_tokens": budget_tokens,
        "elapsed_seconds": elapsed,
        "sql_statement_count": len(statements),
        "sql_write_statement_count": len(writes),
        "physical_reads": env["reads"],
        "selection": packet["selection"],
        "omission_counts": packet["omitted"]["counts"],
        "included_items": len(packet["items"]),
        "estimated_tokens": packet["usage"]["estimated_tokens"],
        "sql_backend": "real PostgreSQL scratch",
        "physical_backend": "controlled in-process strict store",
        "production_sla": False,
    }
    ResourceJournal("startup_scale").event("scale-observation", **observation)
    print("STARTUP_SCALE_OBSERVATION " + json.dumps(observation, sort_keys=True))
    return packet


async def test_inventory_32_64_128_is_scope_first_bounded_read_only_and_has_honest_ceiling(environment: dict[str, Any]) -> None:
    env = environment
    identities = await seed_inventory(env, size=130, confirmed=70, foreign=40)
    for limit in (32, 64, 128):
        packet = await observe(env, inventory_limit=limit)
        assert packet["selection"]["scanned_count"] == limit and packet["selection"]["inventory_truncated"]
        assert packet["selection"]["physical_reads"] == min(limit * 4, 256)
        assert [item["entry_id"] for item in packet["items"]] == identities[: len(packet["items"])]
        assert not packet["selection"]["selection_complete"]
        assert "foreign" not in json.dumps(packet, ensure_ascii=False)
    assert packet["selection"]["read_limited"] and packet["omitted"]["counts"]["physical_read_limit"] == 6


async def test_multiple_source_closures_count_every_actual_read_and_keep_complete_provenance(environment: dict[str, Any]) -> None:
    env = environment
    await seed_inventory(env, size=36, confirmed=34, source_count=3)
    packet = await observe(env, inventory_limit=64, max_items=8)
    assert packet["selection"]["scanned_count"] == 36 and not packet["selection"]["inventory_truncated"]
    assert packet["selection"]["physical_reads"] == 256
    assert packet["omitted"]["counts"]["physical_read_limit"] == 2
    assert packet["omitted"]["counts"]["unverified"] == 2
    assert packet["omitted"]["counts"]["max_items"] == 24
    assert all(len(item["sources"]) == 3 for item in packet["items"])


async def test_budget_and_item_caps_are_output_limits_not_hidden_inventory_claims(environment: dict[str, Any]) -> None:
    env = environment
    await seed_inventory(env, size=32, confirmed=16)
    small = await observe(env, inventory_limit=32, max_items=16, budget_tokens=1024)
    assert small["omitted"]["counts"].get("budget", 0) > 0 and not small["selection"]["selection_complete"]
    one = await observe(env, inventory_limit=32, max_items=1)
    assert len(one["items"]) == 1 and one["omitted"]["counts"]["max_items"] == 15
    assert one["selection"]["scanned_count"] == 32 and one["selection"]["physical_reads"] == 64


async def test_unverified_inventory_performs_no_physical_io_or_model_work(environment: dict[str, Any]) -> None:
    env = environment
    await seed_inventory(env, size=130, confirmed=0)
    packet = await observe(env, inventory_limit=128)
    assert packet["items"] == [] and packet["selection"]["physical_reads"] == 0
    assert packet["omitted"]["counts"] == {"unverified": 128}
    assert env["analyses"] == 0
