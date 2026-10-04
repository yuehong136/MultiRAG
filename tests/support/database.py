"""Scratch database primitives shared by fixture families."""

import json
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import sqlalchemy as sa

from common.config_utils import CONFIGS

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Any:
    """cwd 无关的 alembic 配置（script_location 锚定仓库根）。"""
    from alembic.config import Config

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "configs" / "alembic"))
    return cfg


def _pg_url(dbname: str) -> sa.engine.URL:
    pg = CONFIGS["postgresql"]
    return sa.engine.URL.create(
        "postgresql+psycopg",
        username=pg["user"],
        password=str(pg["password"]),
        host=pg["host"],
        port=int(pg["port"]),
        database=dbname,
    )


def _pg_role_can_create_db(url: sa.engine.URL) -> bool:
    """只读探测：配置的 PG 角色是否有 CREATEDB/superuser 权限。"""
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            return bool(conn.execute(sa.text("SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user")).scalar())
    except Exception:
        return False
    finally:
        engine.dispose()


@contextmanager
def scratch_database(evidence: Path) -> Iterator[sa.Engine]:
    """Bootstrap and always drop the exact owned DB, even after a lost create reply."""
    from alembic import command

    from api.db.db_models import Base

    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"multirag_test_{uuid4().hex[:12]}"
    manifest = {"database": name, "pid": os.getpid(), "created": False, "absent": False, "private_config": None, "containers": []}
    path = evidence / f"{name}.database.json"

    def save() -> None:
        path.write_text(json.dumps(manifest, indent=2))
        path.chmod(0o600)

    admin_url = _pg_url(CONFIGS["postgresql"]["dbname"])
    assert _pg_role_can_create_db(admin_url), "HTTP verification requires isolated scratch database creation"
    with ExitStack() as cleanup:
        cleanup.callback(save)
        admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
        cleanup.callback(admin.dispose)

        def drop() -> None:
            with admin.connect() as connection:
                connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                assert connection.scalar(sa.text("SELECT count(*) FROM pg_database WHERE datname=:name"), {"name": name}) == 0
            manifest["absent"] = True

        cleanup.callback(drop)
        save()
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        manifest["created"] = True
        save()
        engine = sa.create_engine(_pg_url(name))
        cleanup.callback(engine.dispose)
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE SCHEMA usr_ai"))
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            cfg = _alembic_config()
            cfg.attributes["connection"] = connection
            command.stamp(cfg, "head")
        yield engine
