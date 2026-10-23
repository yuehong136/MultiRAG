"""Short, read-only database probes with deterministic resource release."""

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import import_module
from typing import Any

from sqlalchemy import URL, create_engine, text

from common.ssrf_guard import assert_host_is_safe


class DatabaseProbePolicyError(ValueError):
    """A safe validation refusal before driver IO."""


@contextmanager
def _release(action: Callable[[], Any]) -> Iterator[None]:
    """Run cleanup; preserve a probe failure if cleanup also fails."""
    try:
        yield
    except BaseException:
        try:
            action()
        except Exception:
            # Driver messages can contain credentials or connection strings.
            logging.warning("Database probe cleanup failed after a probe error")
        raise
    else:
        action()


def _odbc_value(value: str) -> str:
    return "{" + value.replace("}", "}}") + "}"


def _assert_one(row: Any) -> None:
    if row is None or len(row) != 1 or row[0] != 1:
        raise RuntimeError("Database probe returned an unexpected result.")


def probe_database(*, db_type: str, database: str, username: str, host: str, port: int, password: str) -> None:
    """Validate the destination before importing/connecting any driver."""
    try:
        safe_host = assert_host_is_safe(host)
    except ValueError as error:
        raise DatabaseProbePolicyError(str(error)) from error
    if not 1 <= port <= 65535:
        raise DatabaseProbePolicyError("Invalid database port.")
    if db_type in {"mysql", "mariadb", "oceanbase", "postgres"}:
        url = URL.create(
            "postgresql+psycopg2" if db_type == "postgres" else "mysql+pymysql",
            username=username,
            password=password,
            host=safe_host,
            port=port,
            database=database,
            query={"charset": "utf8mb4"} if db_type == "oceanbase" else {},
        )
        engine = create_engine(url, connect_args={"connect_timeout": 10})
        with _release(engine.dispose):
            conn = engine.connect()
            with _release(conn.close):
                result = conn.execute(text("SELECT 1"))
                with _release(result.close):
                    _assert_one(result.fetchone())
    elif db_type == "mssql":
        pyodbc = import_module("pyodbc")

        server = f"[{safe_host}],{port}" if ":" in safe_host else f"{safe_host},{port}"
        connection_string = f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={_odbc_value(server)};DATABASE={_odbc_value(database)};UID={_odbc_value(username)};PWD={_odbc_value(password)};"
        conn = pyodbc.connect(connection_string, timeout=10)
        with _release(conn.close):
            cursor = conn.cursor()
            with _release(cursor.close):
                cursor.execute("SELECT 1")
                _assert_one(cursor.fetchone())
    elif db_type == "IBM DB2":
        ibm_db = import_module("ibm_db")

        if any(char in database for char in ";{}\x00\r\n"):
            raise DatabaseProbePolicyError("Invalid DB2 database name.")
        # Credentials are separate driver arguments, never DSN fragments.
        connection_string = f"DATABASE={database};HOSTNAME={safe_host};PORT={port};PROTOCOL=TCPIP;CONNECTTIMEOUT=10;"
        conn = ibm_db.connect(connection_string, username, password)
        with _release(lambda: _db2_close(ibm_db, conn)):
            stmt = ibm_db.exec_immediate(conn, "SELECT 1 FROM sysibm.sysdummy1")
            with _release(lambda: _db2_free(ibm_db, stmt)):
                row = ibm_db.fetch_assoc(stmt)
                if not isinstance(row, dict) or list(row.values()) != [1]:
                    raise RuntimeError("Database probe returned an unexpected result.")
    elif db_type == "trino":
        trino = import_module("trino")

        if not database:
            raise DatabaseProbePolicyError("For Trino, database must be catalog.schema or catalog.")
        catalog, separator, schema = database.replace("/", ".", 1).partition(".")
        scheme = "https" if os.environ.get("TRINO_USE_TLS", "0") == "1" else "http"
        auth = trino.auth.BasicAuthentication(username or "ragflow", password) if scheme == "https" and password else None
        conn = trino.dbapi.connect(host=safe_host, port=port, user=username or "ragflow", catalog=catalog, schema=schema if separator else "default", http_scheme=scheme, auth=auth, request_timeout=10)
        with _release(conn.close):
            cursor = conn.cursor()
            with _release(cursor.close):
                cursor.execute("SELECT 1")
                rows = cursor.fetchall()
                if len(rows) != 1:
                    raise RuntimeError("Database probe returned an unexpected result.")
                _assert_one(rows[0])
    else:
        raise DatabaseProbePolicyError("Unsupported database type.")


def _db2_close(driver: Any, conn: Any) -> None:
    if driver.close(conn) is False:
        raise RuntimeError("DB2 connection cleanup failed.")


def _db2_free(driver: Any, stmt: Any) -> None:
    if driver.free_stmt(stmt) is False:
        raise RuntimeError("DB2 statement cleanup failed.")
