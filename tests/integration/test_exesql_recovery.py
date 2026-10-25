"""A real PostgreSQL aborted transaction must recover between tool statements."""

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from agent.tools import exesql
from agent.tools.exesql import ExeSQL, ExeSQLParam
from tests.support.database import scratch_database
from tests.support.resource_journal import ResourceJournal


class PostgreSQLTool(ExeSQL):
    def __init__(self, url: sa.engine.URL) -> None:
        self._param = ExeSQLParam()
        self._param.db_type = "postgres"
        self._param.host = url.host
        self._param.port = url.port
        self._param.database = url.database
        self._param.username = url.username
        self._param.password = url.password

    def check_if_canceled(self, message: str = "") -> bool:
        return False

    def get_input_elements_from_text(self, txt: str) -> dict[str, dict[str, Any]]:
        return {}


def test_aborted_transaction_rollback_keeps_before_and_after_results(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, postgres_service: None) -> None:
    journal = ResourceJournal("exesql-postgres-recovery")
    # Only the destination policy is replaced: this case owns a local scratch DB.
    # The psycopg2 connection, SQL execution, error and rollback remain real.
    with scratch_database(tmp_path) as engine:
        url = engine.url
        monkeypatch.setattr(exesql, "assert_host_is_safe", lambda host: url.host)
        tool = PostgreSQLTool(url)
        tool._invoke(sql="SELECT 1 AS value;SELECT 1/0;SELECT 1 WHERE FALSE;SELECT 2 AS value")
        result = tool.output("json")
        assert result[0] == [{"value": 1}]
        assert "division by zero" in result[1]["content"]
        assert result[2] == {"content": "No record in the database!"}
        assert result[3] == [{"value": 2}]
        with engine.connect() as independent:
            assert independent.scalar(sa.text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND application_name = '' AND pid <> pg_backend_pid()")) == 0
        journal.event("assertions", real_postgres=True, recovered=True, driver_connections_absent=True, destination_policy="explicit local scratch boundary")


def test_read_only_permission_error_recovers_without_mutation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, postgres_service: None) -> None:
    journal = ResourceJournal("exesql-postgres-readonly")
    with scratch_database(tmp_path) as engine:
        monkeypatch.setattr(exesql, "assert_host_is_safe", lambda host: engine.url.host)
        real_connect = exesql.psycopg2.connect

        def connect(**kwargs: Any) -> Any:
            return real_connect(**kwargs, options="-c default_transaction_read_only=on")

        monkeypatch.setattr(exesql.psycopg2, "connect", connect)
        tool = PostgreSQLTool(engine.url)
        journal.event("reserve-sql", database=engine.url.database, table="forbidden_exesql_write", expected_creation=False)
        tool._invoke(sql="SELECT 1 AS value;CREATE TABLE forbidden_exesql_write (id int);SELECT 2 AS value")
        results = tool.output("json")
        assert results[0] == [{"value": 1}] and results[2] == [{"value": 2}]
        assert "read-only transaction" in results[1]["content"]
        with engine.connect() as independent:
            absent = independent.scalar(sa.text("SELECT to_regclass('public.forbidden_exesql_write')")) is None
        assert absent
        journal.event("assertions", real_postgres_readonly=True, denied_table_absent=absent)
