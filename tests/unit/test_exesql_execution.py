"""Statement recovery, cancellation and driver lifecycle at explicit IO seams."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from agent.tools import exesql
from agent.tools.exesql import ExeSQL, ExeSQLParam
from tests.support.resource_journal import ResourceJournal


class SQLTool(ExeSQL):
    def __init__(self, db_type: str = "postgres") -> None:
        self._param = ExeSQLParam()
        self._param.db_type = db_type
        self._param.host = "8.8.8.8"
        self.executed: list[str] = []
        self.cancelled = False

    def check_if_canceled(self, message: str = "") -> bool:
        return self.cancelled

    def get_input_elements_from_text(self, txt: str) -> dict[str, dict[str, Any]]:
        return {}


@pytest.fixture(autouse=True)
def case_journal(request: pytest.FixtureRequest) -> Iterator[ResourceJournal]:
    journal = ResourceJournal(request.node.nodeid)
    journal.event("boundaries", external_services=False, driver="explicit test doubles")
    yield journal
    journal.event("case-complete", external_resources_created=False)


class Cursor:
    description = [("value",)]
    rowcount = 1

    def __init__(self, tool: SQLTool) -> None:
        self.tool = tool
        self.closed = False
        self.rows: list[tuple[int]] = []

    def execute(self, sql: str) -> None:
        self.tool.executed.append(sql)
        if sql == "bad":
            raise ValueError("statement error")
        if sql == "cancel":
            self.tool.cancelled = True
        self.rows = [] if sql == "empty" else [(1,)]
        self.rowcount = len(self.rows)

    def fetchmany(self, size: int) -> list[tuple[int]]:
        return self.rows[:size]

    def close(self) -> None:
        self.closed = True


class Connection:
    def __init__(self, tool: SQLTool) -> None:
        self.statement_cursor = Cursor(tool)
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> Cursor:
        return self.statement_cursor

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def attach(monkeypatch: pytest.MonkeyPatch, tool: SQLTool) -> Connection:
    connection = Connection(tool)
    monkeypatch.setattr(exesql.psycopg2, "connect", lambda **kwargs: connection)
    return connection


def test_statement_error_and_empty_result_keep_surrounding_results(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = SQLTool()
    connection = attach(monkeypatch, tool)
    tool._invoke(sql="first;bad;empty;last")
    results = tool.output("json")
    assert tool.executed == ["first", "bad", "empty", "last"]
    assert results[0] == results[3] == [{"value": 1}]
    assert "SQL Execution Failed: bad\nstatement error" == results[1]["content"]
    assert results[2] == {"content": "No record in the database!"}
    assert connection.rollbacks == 1 and connection.closed and connection.statement_cursor.closed


def test_rollback_failure_is_node_failure_and_closes_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = SQLTool()
    connection = attach(monkeypatch, tool)

    def broken() -> None:
        raise ConnectionError("connection lost")

    monkeypatch.setattr(connection, "rollback", broken)
    with pytest.raises(ConnectionError, match="connection lost"):
        tool._invoke(sql="first;bad;last")
    assert tool.executed == ["first", "bad"]
    assert connection.closed and connection.statement_cursor.closed


def test_connection_failure_is_not_a_statement_result(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = SQLTool()

    def broken(**kwargs: Any) -> None:
        raise ConnectionError("cannot connect")

    monkeypatch.setattr(exesql.psycopg2, "connect", broken)
    with pytest.raises(ConnectionError, match="cannot connect"):
        tool._invoke(sql="first;last")
    assert tool.executed == [] and tool.output("json") == ""


def test_cancellation_and_write_refusal_preserve_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = SQLTool()
    connection = attach(monkeypatch, tool)
    assert tool._invoke(sql="INSERT x;update x;DELETE x;cancel;last") is None
    assert tool.executed == ["cancel"]
    assert connection.closed and connection.statement_cursor.closed


@pytest.mark.parametrize("host", ["127.0.0.1", "169.254.169.254", "10.0.0.1", ""])
def test_unsafe_host_rejected_before_connect(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    tool = SQLTool()
    tool._param.host = host

    def forbidden(**kwargs: Any) -> None:
        pytest.fail("unsafe host opened a connection")

    monkeypatch.setattr(exesql.psycopg2, "connect", forbidden)
    with pytest.raises(ValueError):
        tool._invoke(sql="first")


@pytest.mark.parametrize("cancel", [False, True])
def test_db2_statement_fetch_error_recovery_and_cancellation(monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    tool = SQLTool("IBM DB2")
    state: dict[str, Any] = {"rollbacks": 0, "closed": False, "fetches": 0}

    def execute(conn: object, sql: str) -> str:
        tool.executed.append(sql)
        state["fetches"] = 0
        if sql == "bad":
            raise ValueError("statement error")
        return sql

    def fetch(stmt: str) -> dict[str, int] | bool:
        state["fetches"] += 1
        if stmt == "fetchbad":
            raise ValueError("fetch error")
        if cancel:
            tool.cancelled = True
        return {"value": 1} if state["fetches"] == 1 else False

    def rollback(conn: object) -> None:
        state["rollbacks"] += 1

    driver = SimpleNamespace(connect=lambda *args: object(), exec_immediate=execute, fetch_assoc=fetch, rollback=rollback, close=lambda conn: state.update(closed=True))
    monkeypatch.setattr(exesql, "import_module", lambda name: driver)
    tool._invoke(sql="INSERT x;first;bad;fetchbad;last")
    assert state["closed"]
    if cancel:
        assert tool.executed == ["first"] and state["rollbacks"] == 0
    else:
        assert tool.executed == ["first", "bad", "fetchbad", "last"]
        assert state["rollbacks"] == 2
        assert tool.output("json")[1] == tool.output("json")[-1] == [{"value": 1}]


@pytest.mark.parametrize("failure", ["connect", "rollback"])
def test_db2_connection_and_recovery_failures_are_not_statement_results(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    tool = SQLTool("IBM DB2")
    state = {"closed": False}

    def execute(conn: object, sql: str) -> None:
        tool.executed.append(sql)
        raise ValueError("statement error")

    driver = SimpleNamespace(connect=lambda *args: False if failure == "connect" else object(), exec_immediate=execute, rollback=lambda conn: False, close=lambda conn: state.update(closed=True))
    monkeypatch.setattr(exesql, "import_module", lambda name: driver)
    with pytest.raises(Exception, match=r"Connection Failed|recovery failed"):
        tool._invoke(sql="bad;last")
    assert tool.executed == ([] if failure == "connect" else ["bad"])
    assert state["closed"] is (failure == "rollback")


@pytest.mark.parametrize("state", ["autocommit", "transaction", "transport", "rollback-failure"])
def test_trino_recovery_uses_real_transaction_semantics(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    class QueryError(Exception):
        pass

    tool = SQLTool("trino")
    tool._param.database = "catalog.schema"
    connection = Connection(tool)
    connection.transaction = object() if state in {"transaction", "rollback-failure"} else None
    real_execute = connection.statement_cursor.execute

    def execute(sql: str) -> None:
        if sql == "bad":
            tool.executed.append(sql)
            if state == "transport":
                raise ConnectionError("transport lost")
            raise QueryError("server rejected query")
        real_execute(sql)

    def rollback() -> None:
        if connection.transaction is None:
            raise RuntimeError("no transaction was started")
        if state == "rollback-failure":
            raise ConnectionError("transaction recovery failed")
        connection.rollbacks += 1
        connection.transaction = None

    monkeypatch.setattr(connection.statement_cursor, "execute", execute)
    monkeypatch.setattr(connection, "rollback", rollback)
    driver = SimpleNamespace(dbapi=SimpleNamespace(connect=lambda **kwargs: connection), exceptions=SimpleNamespace(TrinoQueryError=QueryError))
    monkeypatch.setattr(exesql, "import_module", lambda name: driver if name == "trino" else SimpleNamespace(BasicAuthentication=object))
    if state in {"transport", "rollback-failure"}:
        with pytest.raises(ConnectionError):
            tool._invoke(sql="first;bad;last")
        assert tool.executed == ["first", "bad"]
    else:
        tool._invoke(sql="first;bad;last")
        assert tool.output("json")[0] == tool.output("json")[2] == [{"value": 1}]
        assert "server rejected query" in tool.output("json")[1]["content"]
        assert connection.rollbacks == (1 if state == "transaction" else 0)
    assert connection.closed and connection.statement_cursor.closed


def test_unknown_rowcount_empty_fetch_keeps_later_statements(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = SQLTool()
    connection = attach(monkeypatch, tool)
    original = connection.statement_cursor.execute

    def execute(sql: str) -> None:
        original(sql)
        connection.statement_cursor.rowcount = -1

    monkeypatch.setattr(connection.statement_cursor, "execute", execute)
    tool._invoke(sql="first;empty;last")
    assert tool.output("json") == [[{"value": 1}], {"content": "No record in the database!"}, [{"value": 1}]]
    assert connection.rollbacks == 0 and connection.closed
