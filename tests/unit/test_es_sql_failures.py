"""SQL query rejections retain the retry contract without backend error noise."""

import inspect
import logging
from typing import Any
from unittest.mock import Mock

import pytest
from elastic_transport import ConnectionError, ConnectionTimeout
from elasticsearch import BadRequestError

from common.doc_store import es_conn_base
from core.utils.es_conn import ESConnection


@pytest.fixture
def connection() -> Any:
    cls = inspect.getclosurevars(ESConnection).nonlocals["cls"]
    store = object.__new__(cls)
    store.logger = logging.getLogger(__name__)
    store.es = Mock()
    return store


def test_invalid_sql_warns_and_preserves_error(connection: Any, caplog: pytest.LogCaptureFixture) -> None:
    rejected = BadRequestError("unknown_column", meta=Mock(status=400), body={"error": {"type": "verification_exception"}})
    connection.es.sql.query.side_effect = rejected
    with caplog.at_level(logging.WARNING), pytest.raises(Exception, match=r"SQL error:.*unknown_column") as raised:
        connection.sql("SELECT absent FROM chunks", 10, "json")
    assert raised.value.__cause__ is rejected
    assert "SELECT absent FROM chunks" in str(raised.value)
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert all(record.exc_info is None for record in caplog.records)
    connection.es.sql.query.assert_called_once()


def test_connection_failure_keeps_error_traceback(connection: Any, caplog: pytest.LogCaptureFixture) -> None:
    connection.es.sql.query.side_effect = ConnectionError("connection unavailable")
    with caplog.at_level(logging.WARNING), pytest.raises(Exception, match="SQL error: Connection error"):
        connection.sql("SELECT count(*) FROM chunks", 10, "json")
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR
    assert caplog.records[0].exc_info is not None


def test_timeout_keeps_bounded_retry_and_error_level(connection: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    connection.es.sql.query.side_effect = ConnectionTimeout("unavailable")
    reconnect = Mock()
    monkeypatch.setattr(connection, "_connect", reconnect)
    monkeypatch.setattr(es_conn_base.time, "sleep", Mock())
    with caplog.at_level(logging.WARNING):
        assert connection.sql("SELECT count(*) FROM chunks", 10, "json") is None
    assert connection.es.sql.query.call_count == es_conn_base.ATTEMPT_TIME
    assert reconnect.call_count == es_conn_base.ATTEMPT_TIME
    assert all(record.levelno == logging.ERROR for record in caplog.records)


def test_success_returns_original_response(connection: Any) -> None:
    response = {"columns": [{"name": "n"}], "rows": [[1]]}
    connection.es.sql.query.return_value = response
    assert connection.sql("SELECT count(*) AS n FROM chunks", 10, "json") is response
