"""Owned SQL database and Milvus collection for RAPTOR migration contracts."""

import json
import logging
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from pymilvus import DataType, MilvusClient, connections

from api.db.db_models import Base
from common.config_utils import CONFIGS
from core.utils.milvus_conn import MilvusConnection
from tests.support.database import _pg_url


@pytest.fixture(scope="module")
def raptor_storage(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    identifier = uuid4().hex
    database = "raptor_g_" + identifier[:12]
    collection = "raptor_g_" + identifier
    alias = "raptor_g_writer_" + identifier
    report_dir = request.config.getoption("--test-report-dir")
    directory = Path(report_dir) if report_dir else tmp_path_factory.mktemp("raptor-g")
    path = directory / "raptor-resources.json"
    record: dict[str, Any] = {"database": database, "collection": collection, "alias": alias, "database_absent": False, "collection_absent": False, "readbacks": []}

    def save() -> None:
        path.write_text(json.dumps(record, indent=2, default=lambda value: value.tolist() if hasattr(value, "tolist") else str(value)))

    save()  # Exact names recorded before creation.
    config = CONFIGS["milvus"]
    options = {
        "uri": config["hosts"],
        "user": config.get("username", ""),
        "password": config.get("password", ""),
        "db_name": config.get("db_name") or "default",
        "token": config.get("token", ""),
        "timeout": 30,
    }
    with ExitStack() as cleanup:
        admin = sa.create_engine(_pg_url("postgres"), isolation_level="AUTOCOMMIT")
        cleanup.callback(admin.dispose)

        def drop_database() -> None:
            with admin.connect() as conn:
                conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
                record["database_absent"] = conn.scalar(sa.text("SELECT COUNT(*) FROM pg_database WHERE datname=:name"), {"name": database}) == 0
            save()

        cleanup.callback(drop_database)
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{database}"'))
        engine = sa.create_engine(_pg_url(database))
        cleanup.callback(engine.dispose)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE SCHEMA usr_ai"))
        Base.metadata.create_all(engine)
        reader = MilvusClient(**options)
        cleanup.callback(reader.close)

        def drop_collection() -> None:
            if reader.has_collection(collection):
                reader.drop_collection(collection)
            record["collection_absent"] = not reader.has_collection(collection)
            save()

        cleanup.callback(drop_collection)
        cleanup.callback(connections.disconnect, alias)
        connections.connect(alias=alias, **options)
        cls = next(cell.cell_contents for cell in MilvusConnection.__closure__ or [] if isinstance(cell.cell_contents, type))
        store = cls.__new__(cls)
        store.logger = logging.getLogger("test.raptor.migration")
        store._using = alias
        schema = reader.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=512)
        for name in ("id", "doc_id", "kb_id", "content_with_weight", "raptor_kwd"):
            schema.add_field(name, DataType.VARCHAR, max_length=65535)
        schema.add_field("extra", DataType.JSON)
        schema.add_field("raptor_layer_int", DataType.INT64)
        schema.add_field("q_2_vec", DataType.FLOAT_VECTOR, dim=2)
        indexes = reader.prepare_index_params()
        indexes.add_index("q_2_vec", index_type="AUTOINDEX", metric_type="COSINE")
        reader.create_collection(collection, schema=schema, index_params=indexes, consistency_level="Strong")
        record["milvus_version"] = reader.get_server_version()
        save()
        yield {"engine": engine, "store": store, "reader": reader, "collection": collection, "record": record, "save": save}
