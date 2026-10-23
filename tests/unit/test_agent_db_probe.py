"""Driver failure edges release all acquired resources without hiding errors."""

import socket
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from api.apps.services import agent_db_probe as probe
from common import ssrf_guard


class ProbeFailure(RuntimeError):
    pass


def driver_boundary(monkeypatch: pytest.MonkeyPatch, failures: set[str]) -> list[str]:
    calls: list[str] = []

    def call(name: str, result: Any = None) -> Any:
        calls.append(name)
        if name in failures:
            raise ProbeFailure(name)
        return result

    result = SimpleNamespace(close=lambda: call("result.close"), fetchone=lambda: call("fetch", (1,)))
    cursor = SimpleNamespace(close=lambda: call("cursor.close"), execute=lambda *_: call("execute", result), fetchone=lambda: call("fetch", (1,)), fetchall=lambda: call("fetch", [(1,)]))
    conn = SimpleNamespace(close=lambda: call("conn.close"), cursor=lambda: call("cursor", cursor), execute=lambda *_: call("execute", result))
    engine = SimpleNamespace(connect=lambda: call("connect", conn), dispose=lambda: call("dispose"))
    driver = SimpleNamespace(
        connect=lambda *_, **__: call("connect", conn),
        exec_immediate=lambda *_: call("execute", cursor),
        fetch_assoc=lambda *_: call("fetch", {"1": 1}),
        free_stmt=lambda *_: call("stmt.free", True),
        close=lambda *_: call("conn.close", True),
        auth=SimpleNamespace(BasicAuthentication=lambda *_: object()),
    )
    driver.dbapi = driver
    monkeypatch.setattr(probe, "create_engine", lambda *_, **__: engine)
    monkeypatch.setattr(probe, "import_module", lambda _: driver)
    monkeypatch.setattr(probe, "assert_host_is_safe", lambda _: "1.1.1.1")
    return calls


def run(db_type: str) -> None:
    probe.probe_database(db_type=db_type, database="catalog.schema", username="name", host="db.example", port=5432, password="secret")


@pytest.mark.parametrize("db_type", ["mysql", "mariadb", "oceanbase", "postgres"])
@pytest.mark.parametrize("failure", [None, "connect", "execute", "fetch", "result.close", "conn.close", "dispose"])
def test_sqlalchemy_releases_on_every_edge(monkeypatch: pytest.MonkeyPatch, db_type: str, failure: str | None) -> None:
    calls = driver_boundary(monkeypatch, {failure} if failure else set())
    if failure:
        with pytest.raises(ProbeFailure, match=failure):
            run(db_type)
    else:
        run(db_type)
    assert calls[-1] == "dispose"
    if failure != "connect":
        assert "conn.close" in calls
    if failure not in {"connect", "execute"}:
        assert "result.close" in calls


@pytest.mark.parametrize(
    ("db_type", "failure"),
    [
        (driver, failure)
        for driver in ["mssql", "trino", "IBM DB2"]
        for failure in [None, "connect", "cursor", "execute", "fetch", "cursor.close", "conn.close", "stmt.free"]
        if not ((driver == "IBM DB2" and failure in {"cursor", "cursor.close"}) or (driver != "IBM DB2" and failure == "stmt.free"))
    ],
)
def test_native_drivers_release_on_every_edge(monkeypatch: pytest.MonkeyPatch, db_type: str, failure: str | None) -> None:
    calls = driver_boundary(monkeypatch, {failure} if failure else set())
    if failure:
        with pytest.raises(ProbeFailure, match=failure):
            run(db_type)
    else:
        run(db_type)
    if failure != "connect":
        assert calls[-1] == "conn.close"
    if failure not in {"connect", "cursor"}:
        assert "stmt.free" in calls if db_type == "IBM DB2" and failure != "execute" else "cursor.close" in calls or db_type == "IBM DB2"


@pytest.mark.parametrize("db_type", ["mysql", "mariadb", "oceanbase", "postgres", "mssql", "trino", "IBM DB2"])
def test_primary_error_survives_failed_cleanup(monkeypatch: pytest.MonkeyPatch, db_type: str) -> None:
    calls = driver_boundary(monkeypatch, {"execute", "conn.close", "dispose", "cursor.close"})
    with pytest.raises(ProbeFailure, match=r"^execute$"):
        run(db_type)
    assert "conn.close" in calls


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "10.0.0.2", "169.254.169.254", "100.64.0.1"])
def test_probe_rejects_private_destinations_before_driver(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setattr(probe, "create_engine", lambda *_a, **_k: pytest.fail("driver must not be constructed"))
    with pytest.raises(probe.DatabaseProbePolicyError, match="public"):
        probe.probe_database(db_type="postgres", database="db", username="user", host=host, port=5432, password="secret")


@pytest.mark.parametrize("answers", [[], ["1.1.1.1", "10.0.0.1"], ["::ffff:10.0.0.1"]])
def test_host_policy_rejects_all_mixed_empty_or_mapped_answers(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    monkeypatch.setattr(ssrf_guard.socket, "getaddrinfo", lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in answers])
    with pytest.raises(ValueError):
        ssrf_guard.assert_host_is_safe("example.com")


def test_public_host_is_pinned_and_resolution_error_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf_guard.socket, "getaddrinfo", lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))])
    assert ssrf_guard.assert_host_is_safe("db.example") == "1.1.1.1"

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror("resolver secret")

    monkeypatch.setattr(ssrf_guard.socket, "getaddrinfo", fail)
    with pytest.raises(ValueError, match=r"^Could not resolve database host\.$"):
        ssrf_guard.assert_host_is_safe("db.example")


def test_db2_false_cleanup_receipts_are_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    driver_boundary(monkeypatch, set())
    for operation in ["close", "free_stmt"]:
        driver = SimpleNamespace(
            connect=lambda *_: object(),
            exec_immediate=lambda *_: object(),
            fetch_assoc=lambda *_: {"1": 1},
            free_stmt=lambda *_: True,
            close=lambda *_: True,
        )
        setattr(driver, operation, lambda *_: False)
        monkeypatch.setattr(probe, "import_module", lambda _: driver)
        with pytest.raises(RuntimeError, match="cleanup failed"):
            run("IBM DB2")


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/private",
        "http://[::1]:8080/private",
        "http://[::ffff:127.0.0.1]:8080/private",
        "https://db.example:8443/cancel",
        "http://db.example:8081/page",
        "http://db.example:0/page",
        "http://evil.example:8080/page",
        "http://user:password@db.example:8080/page",
        "http://db.example:8080/page#fragment",
    ],
)
def test_trino_every_method_rejects_unsafe_url_before_adapter(monkeypatch: pytest.MonkeyPatch, method: str, url: str) -> None:
    monkeypatch.setattr(requests.Session, "send", lambda *_a, **_k: pytest.fail("unsafe URL reached transport"))
    session = probe._TrinoProbeSession("http", "db.example", 8080, "1.1.1.1")
    with pytest.raises(probe.DatabaseProbePolicyError):
        session.send(requests.Request(method, url).prepare())
    session.close()


def test_trino_cleanup_only_error_closes_session_and_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    session = probe._TrinoProbeSession("http", "db.example", 8080, "1.1.1.1")
    session._cancel_uri = "http://db.example:8080/v1/statement/query"
    closed = []
    response = requests.Response()
    response.status_code = 500
    response._content = b""
    response._content_consumed = True
    monkeypatch.setattr(session, "delete", lambda *_a, **_k: response)
    monkeypatch.setattr(requests.Session, "close", lambda _: closed.append(True))
    with pytest.raises(RuntimeError, match="cancellation failed"):
        session.close()
    assert closed == [True]
    session.close()
    assert closed == [True]
