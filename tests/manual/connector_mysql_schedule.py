"""Explicit real MySQL scheduling acceptance; owns and drops one random DB.

Run with a private JSON config file (host/port/user/password/name); never reads
or modifies a business schema. Only four scheduling tables are materialized:
PostgreSQL JSONB is represented as MySQL JSON, dates as DATETIME(6). This is a
query/session test, not full application schema/migration acceptance.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql

from api.db.db_models import Connector, Connector2Kb, Knowledgebase, SyncLogs, build_database_url, get_engine_config
from tests.support.connector_schedule import assert_schedule_readback, assert_utc_persistence


def accept(config: dict[str, Any], evidence: Path) -> None:
    name = "multirag_schedule_" + uuid4().hex[:12]
    admin_url = sa.make_url(build_database_url(config)).set(database=None)
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", **get_engine_config(config))
    result: dict[str, Any] = {"database": name, "created": False, "absent": False, "schema_scope": "four scheduling tables; JSONB->JSON, DATETIME(6)"}
    engine: sa.Engine | None = None
    try:
        with admin.connect() as connection:
            result["version"] = connection.scalar(sa.text("SELECT VERSION()"))
            assert connection.scalar(sa.text("SELECT @@session.time_zone")) == "+00:00"
            result["global_timezone"] = connection.scalar(sa.text("SELECT @@global.time_zone"))
            connection.execute(sa.text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"))
            result["created"] = True
        engine = sa.create_engine(admin_url.set(database=name), **get_engine_config({**config, "max_connections": 1})).execution_options(schema_translate_map={"usr_ai": name})
        metadata = sa.MetaData()
        for model in (Connector, Connector2Kb, Knowledgebase, SyncLogs):
            table = model.__table__.to_metadata(metadata)
            for column in table.columns:
                if isinstance(column.type, postgresql.JSONB):
                    column.type = sa.JSON()
                elif isinstance(column.type, sa.DateTime):
                    column.type = mysql.DATETIME(fsp=6)
        metadata.create_all(engine)
        result["persistence"] = assert_utc_persistence(engine)
        result["utc_session"] = assert_schedule_readback(engine)
        with engine.connect() as connection:
            connection.execute(sa.text("SET time_zone = '+08:00'"))
            connection.commit()
        result["non_utc_session"] = assert_schedule_readback(engine)
        with engine.connect() as connection:
            for model in (Connector, Connector2Kb, Knowledgebase, SyncLogs):
                assert connection.scalar(sa.select(sa.func.count()).select_from(model.__table__)) == 0
        result["owned_rows_absent"] = True
    finally:
        if engine is not None:
            engine.dispose()
        try:
            with admin.connect() as connection:
                connection.execute(sa.text(f"DROP DATABASE IF EXISTS `{name}`"))
                assert connection.scalar(sa.text("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=:name"), {"name": name}) == 0
                result["absent"] = True
        finally:
            admin.dispose()
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text(json.dumps(result, indent=2) + "\n")
            evidence.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if args.config_file.stat().st_mode & 0o077:
        parser.error("config-file must not be group/world readable")
    try:
        accept(json.loads(args.config_file.read_text()), args.evidence)
    except Exception as exc:
        # DB exception representations can include DSNs or bound parameters.
        print(f"MySQL acceptance failed ({type(exc).__name__}); evidence: {args.evidence}", file=sys.stderr)
        return 1
    print(f"MySQL acceptance passed; evidence: {args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
