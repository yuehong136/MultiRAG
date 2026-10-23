"""Short, read-only database probes with deterministic resource release."""

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import import_module
from typing import Any
from urllib.parse import urlsplit

import requests
from sqlalchemy import URL, create_engine, text

from common.ssrf_guard import assert_host_is_safe, pin_dns


class DatabaseProbePolicyError(ValueError):
    """A safe validation refusal before driver IO."""


class _TrinoProbeSession(requests.Session):
    """Keep every probe request on one origin and one validated IP."""

    def __init__(self, scheme: str, host: str, port: int, ip: str) -> None:
        super().__init__()
        self.trust_env = False
        self.verify = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE") or True
        authority = f"[{host}]" if ":" in host else host
        # Requests canonicalizes case and IDNA before urllib3 resolves a host.
        # The origin and DNS pin must use that exact same transport hostname.
        normalized = urlsplit(requests.Request("GET", f"{scheme}://{authority}:{port}/").prepare().url or "")
        self._host = normalized.hostname or ""
        self._origin = (scheme, self._host, port)
        self._ip = ip
        self._cancel_uri: str | None = None
        self._closed = False

    def _check_url(self, url: str) -> None:
        try:
            raw = urlsplit(url)
            if raw.username is not None or raw.password is not None or raw.fragment:
                raise ValueError
            parsed = urlsplit(requests.Request("GET", url).prepare().url or "")
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
            if (parsed.scheme, parsed.hostname, port) != self._origin:
                raise ValueError
        except (ValueError, requests.RequestException):
            raise DatabaseProbePolicyError("Trino probe refused an unsafe response URL.") from None

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        self._check_url(request.url or "")
        kwargs.update(allow_redirects=False, proxies={})
        with pin_dns(self._host, self._ip):
            response = super().send(request, **kwargs)
        try:
            if 300 <= response.status_code < 400:
                raise DatabaseProbePolicyError("Trino probe does not follow redirects.")
            if request.method in {"POST", "GET"} and response.ok:
                payload = response.json()
                if isinstance(payload, dict):
                    next_uri = payload.get("nextUri")
                    if next_uri is not None:
                        if not isinstance(next_uri, str):
                            raise DatabaseProbePolicyError("Trino probe returned an invalid continuation.")
                        self._check_url(next_uri)
                    self._cancel_uri = next_uri
                    # Spooling creates a separate driver session. A SELECT 1
                    # probe only accepts inline rows, before that code can run.
                    if payload.get("data") is not None and not isinstance(payload["data"], list):
                        raise DatabaseProbePolicyError("Trino probe requires inline query results.")
            elif request.method == "DELETE" and response.status_code == 204 and request.url == self._cancel_uri:
                self._cancel_uri = None
        except BaseException:
            try:
                response.close()
            except Exception:
                logging.warning("Database probe response cleanup failed after a probe error")
            raise
        return response

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with _release(super().close):
            # A rejected response may precede the driver's recording nextUri.
            # Cancel only a target already validated by this session.
            if self._cancel_uri is not None:
                response = self.delete(self._cancel_uri, timeout=10)
                with _release(response.close):
                    if response.status_code != 204:
                        raise RuntimeError("Trino probe cancellation failed.")


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
        session = _TrinoProbeSession(scheme, host, port, safe_host)
        with _release(session.close):
            conn = trino.dbapi.connect(
                host=host, port=port, user=username or "ragflow", catalog=catalog, schema=schema if separator else "default", http_scheme=scheme, auth=auth, request_timeout=10, http_session=session
            )
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
