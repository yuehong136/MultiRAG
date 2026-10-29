"""Real PostgreSQL checks for owner-scoped atomic tag updates and timestamps."""

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db.db_models import Base, CanvasTemplate, UserCanvas, UserCanvasVersion
from api.db.services.canvas_service import CanvasTemplateService, UserCanvasService
from tests.support.resource_journal import ResourceJournal

pytestmark = pytest.mark.integration

OWNER = "after98-A-owner"
FOREIGN_OWNER = "after98-A-foreign"
OWNED_CANVAS = "after98-A-owned"
TEAM_CANVAS = "after98-A-team"
OTHER_CANVAS = "after98-A-other"
STALE_OWNER_CANVAS = "after98-A-stale-owner"
MISSING_CANVAS = "after98-A-missing"
VERSION = "after98-A-version"
CANVAS_IDS = (OWNED_CANVAS, TEAM_CANVAS, OTHER_CANVAS, STALE_OWNER_CANVAS)
OLD_TIMESTAMP = 978307200000
OLD_DATE = datetime(2001, 1, 1)
TEMPLATE_IDS = ("after98-A-template-a", "after98-A-template-b", "after98-A-template-old")


@pytest.fixture
async def atomic_tags_engine(request: pytest.FixtureRequest) -> AsyncIterator[AsyncEngine]:
    """Use a pre-registered batch DB, or the standard scratch fixture in CI."""
    supplied_dsn = os.environ.get("MULTIRAG_AGENT_TAGS_ATOMIC_DSN")
    if supplied_dsn:
        url = make_url(supplied_dsn)
        assert url.drivername == "postgresql+psycopg", "The atomic tag test requires PostgreSQL/psycopg"
        assert url.host and not url.host.startswith("/") and url.port, "Supply the registered TCP endpoint explicitly"
        assert url.database and url.database.startswith("multirag_test_"), "Only an owned scratch database is allowed"
    else:
        scratch: Engine = request.getfixturevalue("pg_scratch_engine")
        url = scratch.url
    journal = ResourceJournal("agent-tags-atomic")
    journal.event("reserve-sql-scope", database=url.database, host=url.host, port=url.port, schema="usr_ai", tables=[UserCanvas.__tablename__, UserCanvasVersion.__tablename__])
    if not supplied_dsn:
        Base.metadata.create_all(scratch, tables=[UserCanvas.__table__, UserCanvasVersion.__table__])
    journal.event("reserve-sql-rows", database=url.database, canvases=list(CANVAS_IDS), versions=[VERSION], owners=[OWNER, FOREIGN_OWNER], missing_canvas_probe=MISSING_CANVAS)
    engine = create_async_engine(url, poolclass=NullPool, connect_args={"options": "-c timezone=UTC"})
    seeded = False
    scope_verified = False
    try:
        async with engine.begin() as connection:
            existing = (await connection.execute(select(UserCanvas.id).where(UserCanvas.id.in_((*CANVAS_IDS, MISSING_CANVAS))))).all()
            assert not existing, "The reserved atomic tag rows already exist"
            assert (await connection.execute(select(UserCanvasVersion.id).where(UserCanvasVersion.id == VERSION))).scalar_one_or_none() is None
            scope_verified = True
            rows: list[dict[str, Any]] = []
            for canvas_id, owner, permission in (
                (OWNED_CANVAS, OWNER, "me"),
                (TEAM_CANVAS, FOREIGN_OWNER, "team"),
                (OTHER_CANVAS, OWNER, "me"),
                (STALE_OWNER_CANVAS, OWNER, "me"),
            ):
                rows.append(
                    {
                        "id": canvas_id,
                        "user_id": owner,
                        "title": canvas_id,
                        "tags": "original",
                        "permission": permission,
                        "release": True,
                        "canvas_category": "agent_canvas",
                        "dsl": {"components": {}, "history": [], "marker": canvas_id},
                        "create_time": OLD_TIMESTAMP,
                        "update_time": OLD_TIMESTAMP,
                        "create_date": OLD_DATE,
                        "update_date": OLD_DATE,
                    }
                )
            await connection.execute(insert(UserCanvas), rows)
            await connection.execute(
                insert(UserCanvasVersion).values(
                    id=VERSION,
                    user_canvas_id=OWNED_CANVAS,
                    title="Published version",
                    release=True,
                    dsl={"components": {}, "marker": "version"},
                    create_time=OLD_TIMESTAMP,
                    update_time=OLD_TIMESTAMP,
                    create_date=OLD_DATE,
                    update_date=OLD_DATE,
                )
            )
        seeded = True
        yield engine
    finally:
        try:
            if seeded:
                async with engine.begin() as connection:
                    await connection.execute(delete(UserCanvasVersion).where(UserCanvasVersion.id == VERSION))
                    await connection.execute(delete(UserCanvas).where(UserCanvas.id.in_(CANVAS_IDS)))
            if scope_verified:
                async with engine.connect() as reader:
                    assert (await reader.execute(select(UserCanvas.id).where(UserCanvas.id.in_(CANVAS_IDS)))).all() == []
                    assert (await reader.execute(select(UserCanvasVersion.id).where(UserCanvasVersion.id == VERSION))).scalar_one_or_none() is None
                journal.event("sql-cleanup-readback", database=url.database, canvases=list(CANVAS_IDS), versions=[VERSION], absent=True)
        finally:
            await engine.dispose()


async def read_rows(engine: AsyncEngine) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read committed results with a fresh connection, outside the write session."""
    async with engine.connect() as reader:
        canvases = (await reader.execute(select(UserCanvas.__table__).where(UserCanvas.id.in_(CANVAS_IDS)))).mappings().all()
        version = (await reader.execute(select(UserCanvasVersion.__table__).where(UserCanvasVersion.id == VERSION))).mappings().one()
        return {str(row["id"]): dict(row) for row in canvases}, dict(version)


async def test_tag_write_updates_timestamps_and_empty_list_clears_only_owned_canvas(atomic_tags_engine: AsyncEngine) -> None:
    before, version_before = await read_rows(atomic_tags_engine)
    async with AsyncSession(atomic_tags_engine) as writer:
        assert await UserCanvasService.update_tags(writer, OWNED_CANVAS, OWNER, [" ML ", "ml", "Data,Science"])
    after, version_after = await read_rows(atomic_tags_engine)
    owned = after[OWNED_CANVAS]
    assert owned["tags"] == "ML,Data Science"
    assert owned["update_time"] > OLD_TIMESTAMP
    assert owned["update_date"] > OLD_DATE
    assert owned["update_date"].replace(tzinfo=UTC) == datetime.fromtimestamp(owned["update_time"] / 1000, tz=UTC)
    unchanged = before[OWNED_CANVAS] | {"tags": owned["tags"], "update_time": owned["update_time"], "update_date": owned["update_date"]}
    assert owned == unchanged
    assert version_after == version_before
    assert {key: value for key, value in after.items() if key != OWNED_CANVAS} == {key: value for key, value in before.items() if key != OWNED_CANVAS}

    async with AsyncSession(atomic_tags_engine) as writer:
        assert await UserCanvasService.update_tags(writer, OWNED_CANVAS, OWNER, [])
    cleared, version_cleared = await read_rows(atomic_tags_engine)
    assert cleared[OWNED_CANVAS]["tags"] == ""
    assert cleared[OWNED_CANVAS]["update_time"] >= owned["update_time"]
    assert cleared[OWNED_CANVAS]["update_date"] >= owned["update_date"]
    expected_cleared = owned | {"tags": "", "update_time": cleared[OWNED_CANVAS]["update_time"], "update_date": cleared[OWNED_CANVAS]["update_date"]}
    assert cleared[OWNED_CANVAS] == expected_cleared
    assert version_cleared == version_before
    assert {key: value for key, value in cleared.items() if key != OWNED_CANVAS} == {key: value for key, value in before.items() if key != OWNED_CANVAS}


async def test_foreign_team_and_missing_agents_have_zero_write_side_effects(atomic_tags_engine: AsyncEngine) -> None:
    before = await read_rows(atomic_tags_engine)
    for canvas_id, caller in ((OWNED_CANVAS, FOREIGN_OWNER), (TEAM_CANVAS, OWNER), (MISSING_CANVAS, OWNER)):
        async with AsyncSession(atomic_tags_engine) as writer:
            assert not await UserCanvasService.update_tags(writer, canvas_id, caller, ["forbidden"])
            await writer.commit()
        assert await read_rows(atomic_tags_engine) == before


async def test_owner_change_after_preflight_cannot_authorize_a_stale_write(atomic_tags_engine: AsyncEngine) -> None:
    async with AsyncSession(atomic_tags_engine) as stale_writer:
        assert await stale_writer.scalar(select(UserCanvas.user_id).where(UserCanvas.id == STALE_OWNER_CANVAS)) == OWNER
        async with AsyncSession(atomic_tags_engine) as concurrent_writer:
            await concurrent_writer.execute(update(UserCanvas).where(UserCanvas.id == STALE_OWNER_CANVAS).values(user_id=FOREIGN_OWNER))
            await concurrent_writer.commit()
        before = await read_rows(atomic_tags_engine)
        assert not await UserCanvasService.update_tags(stale_writer, STALE_OWNER_CANVAS, OWNER, ["forbidden"])
        await stale_writer.commit()
    assert await read_rows(atomic_tags_engine) == before


@pytest.fixture
def catalog_engine(request: pytest.FixtureRequest) -> Iterator[Engine]:
    """Borrow the batch's declared template table, or create it in CI scratch."""
    supplied_dsn = os.environ.get("MULTIRAG_AGENT_TAGS_ATOMIC_DSN")
    if supplied_dsn:
        url = make_url(supplied_dsn)
        assert url.drivername == "postgresql+psycopg", "The template test requires PostgreSQL/psycopg"
        assert url.host and not url.host.startswith("/") and url.port, "Supply the registered TCP endpoint explicitly"
        assert url.database and url.database.startswith("multirag_test_"), "Only an owned scratch database is allowed"
        engine = create_engine(url, poolclass=NullPool, connect_args={"options": "-c timezone=UTC"})
    else:
        engine = request.getfixturevalue("pg_scratch_engine")
    journal = ResourceJournal("agent-template-catalog-schema")
    journal.event("reserve-sql-scope", database=engine.url.database, host=engine.url.host, port=engine.url.port, schema="usr_ai", tables=[CanvasTemplate.__tablename__])
    if not supplied_dsn:
        Base.metadata.create_all(engine, tables=[CanvasTemplate.__table__])
    try:
        yield engine
    finally:
        if supplied_dsn:
            engine.dispose()


def test_template_catalog_preserves_locale_dsl_and_stable_order(catalog_engine: Engine) -> None:
    journal = ResourceJournal("agent-template-catalog-rows")
    journal.event("reserve-sql-rows", database=catalog_engine.url.database, host=catalog_engine.url.host, port=catalog_engine.url.port, table=CanvasTemplate.__tablename__, ids=list(TEMPLATE_IDS))
    title = {"zh": "标题", "en": "Title", "fr": ""}
    description = {"en": "English fallback"}
    dsl = {"components": {"starter": {"params": {"count": 0, "values": []}}}, "history": [], "enabled": False}
    seeded = False
    scope_verified = False
    try:
        with Session(catalog_engine) as writer:
            assert CanvasTemplateService.list_templates(writer) == [], "The isolated template catalogue must start empty"
            scope_verified = True
            for template_id in (TEMPLATE_IDS[1], TEMPLATE_IDS[0], TEMPLATE_IDS[2]):
                writer.execute(
                    insert(CanvasTemplate).values(
                        id=template_id,
                        title=title,
                        description=description,
                        dsl=dsl,
                        canvas_type="recommended",
                        canvas_types=["recommended"],
                        canvas_category="agent_canvas",
                        create_time=OLD_TIMESTAMP if template_id == TEMPLATE_IDS[2] else OLD_TIMESTAMP + 1,
                    )
                )
            writer.commit()
        seeded = True
        with Session(catalog_engine) as reader:
            rows = CanvasTemplateService.list_templates(reader)
            assert [row.id for row in rows] == list(TEMPLATE_IDS)
            for row in rows:
                assert row.title == title
                assert row.description == description
                assert row.dsl == dsl
                assert row.canvas_type == "recommended" and row.canvas_types == ["recommended"]
        with catalog_engine.connect() as reader:
            persisted = reader.execute(select(CanvasTemplate.__table__).where(CanvasTemplate.id.in_(TEMPLATE_IDS))).mappings().all()
            assert {row["id"] for row in persisted} == set(TEMPLATE_IDS)
            assert all(row["title"] == title and row["description"] == description and row["dsl"] == dsl for row in persisted)
    finally:
        if seeded:
            with catalog_engine.begin() as cleanup:
                cleanup.execute(delete(CanvasTemplate).where(CanvasTemplate.id.in_(TEMPLATE_IDS)))
        if scope_verified:
            with catalog_engine.connect() as reader:
                assert reader.execute(select(CanvasTemplate.id).where(CanvasTemplate.id.in_(TEMPLATE_IDS))).all() == []
            with Session(catalog_engine) as reader:
                assert CanvasTemplateService.list_templates(reader) == []
            journal.event("sql-cleanup-readback", database=catalog_engine.url.database, table=CanvasTemplate.__tablename__, ids=list(TEMPLATE_IDS), absent=True)
