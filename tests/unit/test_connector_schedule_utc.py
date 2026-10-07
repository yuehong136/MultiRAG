from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import literal
from sqlalchemy.dialects import mysql, postgresql

from api.db.db_models import get_engine_config
from api.db.services.connector_service import _schedule_time_condition


@pytest.mark.parametrize("name", ["postgresql", "postgresql+psycopg", "mysql", "mysql+pymysql", "mysql+mysqlconnector", "oceanbase"])
def test_utc_connection_policy_for_qualified_drivers(name: str) -> None:
    args: dict[str, Any] = get_engine_config({"name": name})["connect_args"]
    if name.startswith("postgresql"):
        assert "timezone=UTC" in args["options"] and "statement_timeout=30000" in args["options"]
    else:
        assert args["init_command"] == "SET time_zone = '+00:00'"


@pytest.mark.parametrize("name, dialect", [("postgresql", postgresql.dialect()), ("mysql", mysql.dialect())])
def test_schedule_has_typed_interval_no_minute_rounding(name: str, dialect: Any) -> None:
    compiled = str(_schedule_time_condition(name).compile(dialect=dialect))
    assert "timestampdiff" not in compiled.lower() and "||" not in compiled
    assert " < " in compiled
    assert "timezone" in compiled if name == "postgresql" else "utc_timestamp" in compiled
    with_reference = str(_schedule_time_condition(name, literal(datetime(2026, 1, 1, tzinfo=UTC))).compile(dialect=dialect))
    assert "now()" not in with_reference.lower() and "utc_timestamp" not in with_reference.lower()


def test_unsupported_scheduler_backend_fails_explicitly() -> None:
    with pytest.raises(ValueError, match="sqlite"):
        _schedule_time_condition("sqlite")
