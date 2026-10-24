"""Actual PostgreSQL/MySQL source tables, isolated from all business schemas."""

import json
import os
import secrets
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.db.db_models import build_database_url, get_engine_config
from common.config_utils import CONFIGS


@pytest.fixture
def rdbms_source(pg_scratch_engine: sa.Engine, tmp_path: Path) -> Iterator[dict[str, Any]]:
    backend = os.environ.get("RDBMS_TEST_SOURCE", "postgresql")
    assert backend in {"postgresql", "mysql"}
    evidence = Path(os.environ.get("RDBMS_EVIDENCE", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {"source": backend, "absent": False}
    path = evidence / f"source-{uuid4().hex}.json"
    with ExitStack() as cleanup:

        def save() -> None:
            path.write_text(json.dumps(record, default=str, indent=2))
            path.chmod(0o600)

        cleanup.callback(save)
        if backend == "mysql":
            private = Path(os.environ["RDBMS_MYSQL_CONFIG_FILE"])
            assert private.stat().st_mode & 0o077 == 0
            raw = json.loads(private.read_text())
            config = {**raw, "name": "mysql+pymysql"}
            admin_url = sa.make_url(build_database_url(config)).set(database=None)
            admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", **get_engine_config(config))
            cleanup.callback(admin.dispose)
            name = "multirag_rdbms_" + uuid4().hex[:12]

            def drop() -> None:
                with admin.connect() as connection:
                    connection.execute(sa.text(f"DROP DATABASE IF EXISTS `{name}`"))
                    assert connection.scalar(sa.text("SELECT count(*) FROM information_schema.schemata WHERE schema_name=:name"), {"name": name}) == 0
                record["absent"] = True

            cleanup.callback(drop)
            record["database"] = name
            save()
            with admin.connect() as connection:
                connection.execute(sa.text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"))
            engine = sa.create_engine(admin_url.set(database=name), **get_engine_config(config))
            cleanup.callback(engine.dispose)
            credentials = {"username": raw["user"], "password": raw["password"]}
            config = {"host": raw["host"], "port": raw["port"], "database": name}
        else:
            engine = pg_scratch_engine
            raw = CONFIGS["postgresql"]
            from psycopg import sql

            role = "mrag_rdbms_" + uuid4().hex[:12]
            password = secrets.token_urlsafe(24)

            def remove_role() -> None:
                with engine.begin() as connection:
                    if connection.scalar(sa.text("SELECT count(*) FROM pg_roles WHERE rolname=:role"), {"role": role}):
                        connection.exec_driver_sql(f'DROP OWNED BY "{role}"')
                        connection.exec_driver_sql(f'DROP ROLE "{role}"')
                    assert connection.scalar(sa.text("SELECT count(*) FROM pg_roles WHERE rolname=:role"), {"role": role}) == 0
                record["role_absent"] = True

            cleanup.callback(remove_role)
            record.update(role=role, database=engine.url.database)
            save()
            native = engine.raw_connection()
            try:
                with native.cursor() as cursor:
                    cursor.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
                native.commit()
            finally:
                native.close()
            credentials = {"username": role, "password": password}
            config = {"host": raw["host"], "port": raw["port"], "database": engine.url.database}
            record["database"] = engine.url.database
        table = sa.Table(
            "rdbms_" + uuid4().hex[:12],
            sa.MetaData(),
            sa.Column("id", sa.Integer),
            sa.Column("body", sa.Text),
            sa.Column("category", sa.Text),
            sa.Column("payload", sa.LargeBinary),
            sa.Column("seq", sa.Numeric(30, 6)),
            sa.Column("active", sa.Boolean),
            sa.Column("stamp", sa.DateTime(timezone=True)),
        )

        def remove_table() -> None:
            table.drop(engine, checkfirst=True)
            assert table.name not in sa.inspect(engine).get_table_names()
            record["table_absent"] = True
            if backend == "postgresql":
                record["absent"] = True

        cleanup.callback(remove_table)
        record["table"] = table.name
        save()
        table.create(engine)
        if backend == "postgresql":
            with engine.begin() as connection:
                connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA public TO "{role}"')
                connection.exec_driver_sql(f'GRANT SELECT ON "{table.name}" TO "{role}"')

        with engine.begin() as connection:
            connection.execute(
                table.insert(),
                [
                    {"id": 1, "body": "retained", "category": "a", "payload": b"binary\x00\xff", "seq": Decimal("1.000001"), "active": True, "stamp": datetime(2026, 1, 1, tzinfo=UTC)},
                    {"id": 2, "body": "stale", "category": "b", "payload": b"binary\x00\xff", "seq": Decimal("2.000002"), "active": True, "stamp": datetime(2026, 1, 2, tzinfo=UTC)},
                ],
            )
        quote = "`" if backend == "mysql" else '"'
        configuration = {
            **config,
            "query": f"SELECT * FROM {quote}{table.name}{quote} WHERE active = true ORDER BY id LIMIT 100",
            "content_columns": "body",
            "metadata_columns": "category,seq",
            "id_column": "id",
            "timestamp_column": "seq",
            "batch_size": 1,
            "credentials": credentials,
            "sync_deleted_files": True,
        }
        save()
        yield {"engine": engine, "table": table, "config": configuration, "backend": backend, "record": record}
