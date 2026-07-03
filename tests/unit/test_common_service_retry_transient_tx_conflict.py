from unittest.mock import MagicMock

import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session

from api.db.services.common_service import retry_transient_tx_conflict


class _PgConflictError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _MySQLConflictError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code, "deadlock")
        self.args = (code, "deadlock")


def _build_dbapi_error(orig: Exception) -> sa_exc.OperationalError:
    return sa_exc.OperationalError("SELECT 1", {}, orig)


class _SessionDouble(Session):
    def __init__(self) -> None:
        super().__init__()
        self.rollback_mock = MagicMock()

    def rollback(self) -> None:
        self.rollback_mock()


def test_retry_transient_tx_conflict_retries_postgres_and_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _SessionDouble()
    attempts = {"count": 0}

    monkeypatch.setattr("api.db.services.common_service.time.sleep", lambda _seconds: None)

    @retry_transient_tx_conflict(max_attempts=3, base_delay=0.0, max_delay=0.0)
    def flaky_operation(db: MagicMock) -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise _build_dbapi_error(_PgConflictError("40P01"))
        return "ok"

    assert flaky_operation(db) == "ok"
    assert attempts["count"] == 2
    db.rollback_mock.assert_called_once()


def test_retry_transient_tx_conflict_retries_mysql_deadlock(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _SessionDouble()
    attempts = {"count": 0}

    monkeypatch.setattr("api.db.services.common_service.time.sleep", lambda _seconds: None)

    @retry_transient_tx_conflict(max_attempts=3, base_delay=0.0, max_delay=0.0)
    def flaky_operation(db: MagicMock) -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise _build_dbapi_error(_MySQLConflictError(1213))
        return "ok"

    assert flaky_operation(db) == "ok"
    assert attempts["count"] == 2
    db.rollback_mock.assert_called_once()


def test_retry_transient_tx_conflict_does_not_retry_non_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _SessionDouble()
    attempts = {"count": 0}

    monkeypatch.setattr("api.db.services.common_service.time.sleep", lambda _seconds: None)

    @retry_transient_tx_conflict(max_attempts=3, base_delay=0.0, max_delay=0.0)
    def failing_operation(db: MagicMock) -> str:
        attempts["count"] += 1
        raise _build_dbapi_error(_PgConflictError("23505"))

    with pytest.raises(sa_exc.OperationalError):
        failing_operation(db)

    assert attempts["count"] == 1
    db.rollback_mock.assert_not_called()
