"""Actual Web MSSQL configuration CRUD; source SQL Server is not contacted."""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db.db_models import Connector
from tests.support.after5e_web_consumers import after5e_consumer_http as after5e_consumer_http
from tests.support.after5e_web_consumers import run_consumer

pytestmark = pytest.mark.external_consumer


def test_mssql_configuration_real_web_and_sql(after5e_consumer_http: dict[str, Any]) -> None:
    env = after5e_consumer_http
    name = "After5e MSSQL " + uuid4().hex
    foreign_id = uuid4().hex
    initial = {
        "host": "mssql.fixture.invalid",
        "port": 1433,
        "database": "owned_source",
        "query": "SELECT id, body, seq FROM [dbo].[rows] ORDER BY id",
        "content_columns": "body",
        "metadata_columns": "",
        "id_column": "id",
        "timestamp_column": "seq",
        "batch_size": 100,
        "credentials": {"username": "readonly", "password": "synthetic-fixture-password"},
        "sync_deleted_files": False,
        "custom_zero": 0,
        "custom_false": False,
    }
    updated = {**initial, "query": "SELECT TOP (2) id, body, seq FROM [dbo].[rows] ORDER BY id", "batch_size": 25}
    env["journal"].event(
        "reserve-web-created-connector",
        database=env["engine"].url.database,
        tenant_id=env["owners"][0],
        name=name,
        source="mssql",
        generated_id_reservation="engine before_cursor_execute reserve-sql-write",
        source_boundary="configuration CRUD only; no TDS, DBAPI probe or source server",
    )
    env["journal"].event("reserve-foreign-connector", database=env["engine"].url.database, id=foreign_id, tenant_id=env["owners"][1])
    with Session(env["engine"]) as db:
        db.add(
            Connector(
                id=foreign_id, tenant_id=env["owners"][1], name="Preserved peer connector", source="mssql", input_type="poll", config={"foreign": True}, refresh_freq=0, prune_freq=0, timeout_secs=30
            )
        )
        db.commit()

    def readback() -> dict[str, Any] | None:
        with Session(env["engine"]) as reader:
            rows = list(reader.scalars(select(Connector).where(Connector.tenant_id == env["owners"][0], Connector.name == name)))
            if not rows or rows[0].config != updated:
                return None
            assert len(rows) == 1
            connector = rows[0]
            assert connector.source == "mssql" and connector.refresh_freq == 0 and connector.prune_freq == 0
            assert type(connector.config["port"]) is int and connector.config["port"] == 1433
            assert connector.config["custom_zero"] == 0 and type(connector.config["custom_zero"]) is int
            assert connector.config["custom_false"] is False and connector.config["sync_deleted_files"] is False
            identifier = connector.id
        return {"connectorId": identifier, "connectorName": name, "source": "mssql", "exists": True, "config": updated}

    result = run_consumer(
        env,
        "verify-mssql-connector-routes.ts",
        {
            "connectorName": name,
            "initialConfig": initial,
            "updatedConfig": updated,
            "refreshFreq": 0,
            "pruneFreq": 0,
            "queryRequired": True,
            "sourceBoundary": "MSSQL config CRUD; no live SQL Server",
            "deletionOwned": {"authorized": True, "connectorName": name, "resourceOwner": "I", "purpose": "scratch fixture exact deletion"},
        },
        readback,
    )
    identifier = result["readback"]["connectorId"]
    with Session(env["engine"]) as reader:
        assert reader.get(Connector, identifier) is None
        peer = reader.get(Connector, foreign_id)
        assert peer is not None and peer.tenant_id == env["owners"][1] and peer.config == {"foreign": True}
    env["journal"].event("mssql-final-independent-sql-readback", connector_id=identifier, deleted_absent=True, foreign_connector_id=foreign_id, foreign_preserved=True, source_server_verified=False)
    env["manifest"]("mssql-consumer-readback", web_sha=result["web_sha"], connector_id=identifier, deleted_absent=True, foreign_preserved=True, source="mssql", source_server_verified=False)
