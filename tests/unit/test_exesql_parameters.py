"""Protected database parameter refusals must survive diagnostic text changes."""

from typing import Any

import pytest

from agent.canvas import Canvas
from agent.tools import exesql
from agent.tools.exesql import ExeSQL, ExeSQLParam


@pytest.mark.parametrize(("host", "password"), [("ragflow-mysql", "external-password"), ("external-db", "infini_rag_flow")])
def test_protected_database_refused_before_tool_construction(monkeypatch: pytest.MonkeyPatch, host: str, password: str) -> None:
    calls: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> None:
        calls.append("database connection")
        pytest.fail("refused tool must never open a database connection")

    for driver in (exesql.pymysql, exesql.psycopg2, exesql.pyodbc):
        monkeypatch.setattr(driver, "connect", forbidden)
    param = ExeSQLParam()
    param.database = "rag_flow"
    param.username = "external-user"
    param.host = host
    param.password = password
    # Tool construction checks parameters before using any Canvas runtime state.
    canvas = object.__new__(Canvas)
    with pytest.raises(ValueError, match=r"^For the security reason, it does not support database named rag_flow\.$"):
        ExeSQL(canvas, "sql", param)
    assert calls == []


@pytest.mark.parametrize(("database", "host", "password"), [("rag_flow", "external-db", "external-password"), ("external", "ragflow-mysql", "infini_rag_flow")])
def test_other_database_parameter_combinations_remain_valid(database: str, host: str, password: str) -> None:
    param = ExeSQLParam()
    param.database = database
    param.username = "external-user"
    param.host = host
    param.password = password
    assert param.check() is None
