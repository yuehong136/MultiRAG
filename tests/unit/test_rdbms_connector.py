import asyncio
import hashlib
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from common.data_source.exceptions import ConnectorValidationError
from common.data_source.rdbms_connector import RDBMSConnector
from core.svr import sync_data_source as sync
from tests.unit.test_sync_deleted_snapshot_contract import sync_env as sync_env
from tests.unit.test_sync_deleted_snapshot_contract import task


def connector(**kwargs: Any) -> RDBMSConnector:
    return RDBMSConnector(
        **(
            {
                "db_type": "postgresql",
                "host": "db",
                "port": 5432,
                "database": "owned",
                "query": "SELECT * FROM t WHERE enabled ORDER BY id LIMIT 2;",
                "content_columns": "body",
                "metadata_columns": "category",
                "id_column": "id",
                "timestamp_column": "seq",
                "batch_size": 1,
            }
            | kwargs
        )
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        0,
        False,
        123456789123456789,
        1.25,
        "a'\\b",
        datetime(2026, 1, 1, 8, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8))),
        datetime(2026, 1, 1),
        date(2026, 1, 1),
        Decimal("123456789123456789.000001"),
        b"\x00\xff",
        UUID(int=12),
    ],
)
def test_typed_cursor_json_roundtrip(value: Any) -> None:
    import json

    encoded = json.loads(json.dumps(RDBMSConnector.serialize_cursor_value(value)))
    result = RDBMSConnector.deserialize_cursor_value(encoded)
    assert result == (value.replace(tzinfo=UTC) if isinstance(value, datetime) and value.tzinfo is None else value)
    assert type(result) is type(value)


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), Decimal("NaN"), {"unknown": 1}, {"__ragflow_rdbms_cursor_type__": "decimal", "value": "NaN"}, {"__ragflow_rdbms_cursor_type__": "bytes", "value": "!"}]
)
def test_invalid_cursor_fails_closed(value: Any) -> None:
    with pytest.raises(ConnectorValidationError):
        RDBMSConnector.deserialize_cursor_value(value)


def test_query_scope_is_wrapped_and_cursor_is_bound() -> None:
    query, parameters = connector(timestamp_column='seq"field')._build_time_filtered_query("SELECT * FROM t WHERE enabled ORDER BY id LIMIT 2", "x' OR 1=1 --", "z")
    assert query == 'SELECT * FROM (SELECT * FROM t WHERE enabled ORDER BY id LIMIT 2) AS ragflow_src WHERE ragflow_src."seq""field" >= %s AND ragflow_src."seq""field" <= %s'
    assert parameters == ("x' OR 1=1 --", "z")
    query, parameters = connector(db_type="mysql")._build_time_filtered_query("SELECT * FROM t", datetime(2026, 1, 1, tzinfo=UTC))
    assert "`seq` >= %s" in query and parameters == (datetime(2026, 1, 1),)


def test_shared_hash_and_metadata_for_typed_rows() -> None:
    result = connector(id_column=None)
    row = {"id": None, "body": {"value": Decimal("1.23"), "date": date(2026, 1, 1)}, "category": ["a", "b"], "seq": datetime(2026, 1, 1, tzinfo=UTC)}
    document = result._row_to_document(row)
    assert document.id == f"DatabaseType.POSTGRESQL:owned:{hashlib.md5(document.blob).hexdigest()}"
    assert document.metadata == {"category": '["a", "b"]'}
    assert document.id == result._document_id(row, result._content(row))
    assert connector()._document_id({"id": 0}, "x").endswith(":0")
    assert connector()._document_id({"id": memoryview(b"\x00\xff")}, "x").endswith(":base64:AP8=")


@pytest.mark.parametrize("mode", ["complete", "empty", "failure", "disabled", "first", "reindex"])
async def test_coordinator_keeps_strict_snapshot_semantics(sync_env: dict[str, Any], mode: str) -> None:
    from common.data_source.models import SlimDocument

    class Source:
        timestamp_column = None
        pending_cursor = None
        scope_signature = None

        def __init__(self, **kwargs: Any) -> None:
            pass

        def load_credentials(self, credentials: dict[str, Any]) -> None:
            pass

        def load_from_state(self) -> Any:
            return iter(())

        def retrieve_all_slim_docs_perm_sync(self) -> Any:
            if mode != "empty":
                yield [SlimDocument(id="retained")]
            if mode == "failure":
                raise PermissionError("later query failed")

    patch = sync_env["monkeypatch"]
    patch.setattr(sync, "RDBMSConnector", Source)
    current = task()
    if mode in {"complete", "empty", "failure"}:
        current["task_type"] = "prune"
    if mode == "first":
        current["poll_range_start"] = None
    if mode == "reindex":
        current["reindex"] = "1"
    driver = sync.MySQL({"database": "source", "credentials": {"username": "u", "password": "p"}, "sync_deleted_files": mode != "disabled"})
    if mode == "failure":
        with pytest.raises(PermissionError):
            await driver._run_task_logic(current)
        assert not sync_env["calls"]
    else:
        await driver._run_task_logic(current)
        if mode in {"disabled", "first", "reindex"}:
            assert not sync_env["calls"]
        else:
            assert sync_env["calls"]


@pytest.mark.parametrize(
    "grants, allowed",
    [
        (["GRANT SELECT ON `owned`.* TO `reader`@`%`"], True),
        (["GRANT ALL PRIVILEGES ON *.* TO `reader`@`%`"], True),
        (["GRANT SELECT ON `owned`.`only_one_table` TO `reader`@`%`"], False),
        (["GRANT SELECT ON *.* TO `reader`@`%`", "REVOKE SELECT ON `owned`.* FROM `reader`@`%`"], False),
    ],
)
def test_mysql_discovery_rejects_permission_filtered_inventory(grants: list[str], allowed: bool) -> None:
    class Cursor:
        command = ""

        def execute(self, command: str) -> None:
            self.command = command

        def fetchall(self) -> list[tuple[str]]:
            return [(row,) for row in grants] if self.command.startswith("SHOW GRANTS") else [("records",)]

    result = connector(db_type="mysql", query="")
    if allowed:
        assert result._base_queries(Cursor()) == ["SELECT * FROM `records`"]
    else:
        with pytest.raises(ConnectorValidationError, match="discovery"):
            result._base_queries(Cursor())


def test_binary_cursor_is_canonical_and_exact() -> None:
    for value in [memoryview(b"\x00\xff"), bytearray(b"\x00\xff")]:
        assert RDBMSConnector.deserialize_cursor_value(RDBMSConnector.serialize_cursor_value(value)) == b"\x00\xff"


def test_memoryview_content_has_stable_hash_and_title() -> None:
    result = connector(id_column=None)
    rows = [{"body": memoryview(b"\x00\xff"), "category": b"\x00\xff"} for _ in range(2)]
    docs = [result._row_to_document(row) for row in rows]
    assert docs[0].id == docs[1].id
    assert docs[0].blob == docs[1].blob and docs[0].semantic_identifier == docs[1].semantic_identifier
    assert docs[0].metadata == {"category": '{"base64": "AP8="}'}


@pytest.mark.parametrize("db_type", ["postgresql", "mssql"])
@pytest.mark.parametrize("mapping", [False, True])
@pytest.mark.parametrize("failure", [None, "read", "cancel", "close"])
def test_stream_batches_and_resource_closure_preserve_failures(monkeypatch: pytest.MonkeyPatch, mapping: bool, failure: str | None, db_type: str) -> None:
    closed: list[str] = []
    error = asyncio.CancelledError("controlled cancellation") if failure == "cancel" else RuntimeError(f"controlled {failure} failure")
    columns = ["id", "body", "category", "seq"]

    class Cursor:
        description = [(name,) for name in columns]

        def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> None:
            pass

        def __iter__(self) -> Iterator[Any]:
            for identifier in (1, 2):
                if failure in {"read", "cancel"} and identifier == 2:
                    raise error
                row = (identifier, f"record {identifier}", "owned", Decimal(identifier))
                yield dict(zip(columns, row, strict=True)) if mapping else row

        def close(self) -> None:
            closed.append("cursor")
            if failure == "close":
                raise error

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            closed.append("connection")

    result = connector(db_type=db_type)
    monkeypatch.setattr(result, "_get_connection", Connection)
    stream = result.load_from_state()
    first = next(stream)
    assert len(first) == 1 and first[0].id.endswith(":1")
    if failure:
        with pytest.raises(type(error)) as raised:
            list(stream)
        assert raised.value is error
    else:
        remaining = list(stream)
        assert [len(batch) for batch in remaining] == [1]
        assert remaining[0][0].id.endswith(":2")
    assert closed == ["cursor", "connection"]


@pytest.mark.parametrize("db_type", ["mysql", "postgresql", "mssql"])
@pytest.mark.parametrize("prefix", ["```sql\n{}\n```", "sql\n{}", "{}"])
def test_query_paste_cleanup_preserves_selection_and_comments(db_type: str, prefix: str) -> None:
    query = "SELECT * FROM t WHERE body LIKE '%ORDER BY%' -- keep scope\nORDER BY id OFFSET 2 ROWS FETCH NEXT 3 ROWS ONLY"
    result = connector(db_type=db_type, query=prefix.format(query))
    assert result.query == query
    wrapped = result._wrap_query(result.query)
    assert query in wrapped
    assert "OFFSET 0" not in wrapped


@pytest.mark.parametrize(
    "query, append_offset",
    [
        ("SELECT * FROM t ORDER BY id", True),
        ("SELECT TOP (2) * FROM t ORDER BY id", False),
        ("SELECT TOP (2) WITH TIES * FROM t ORDER BY id", False),
        ("SELECT * FROM t ORDER BY id OFFSET 2 ROWS FETCH NEXT 3 ROWS ONLY", False),
        ("SELECT ROW_NUMBER() OVER (ORDER BY id) AS rank, body FROM t", False),
        ("SELECT 'ORDER BY ( TOP' AS body FROM t ORDER BY id -- TOP (1)", True),
        ("SELECT [ORDER BY]], TOP] AS body FROM t /* ORDER BY /* inner */ TOP */ ORDER BY id", True),
        ("SELECT * FROM (SELECT TOP (2) * FROM t ORDER BY id) AS nested ORDER BY id", True),
        ("SELECT * FROM t -- ORDER BY id\n", False),
        ("SELECT * FROM t ORDER BY id; -- trailing comment", True),
    ],
)
def test_mssql_wrapping_keeps_top_pagination_windows_literals_and_comments(query: str, append_offset: bool) -> None:
    wrapped = connector(db_type="mssql")._wrap_query(query)
    assert ("OFFSET 0 ROWS" in wrapped) is append_offset
    assert wrapped.endswith("\n) AS ragflow_src")
    assert query.replace(";", "") in wrapped


@pytest.mark.parametrize("query", ["SELECT * INTO stolen FROM t", "DELETE FROM t", "SELECT * FROM t; DELETE FROM t", "SELECT (body FROM t", "SELECT body FROM t /* unfinished"])
def test_mssql_rejects_non_select_and_multiple_statements(query: str) -> None:
    with pytest.raises(ConnectorValidationError):
        connector(db_type="mssql")._wrap_query(query)


def test_mssql_parameter_binding_preserves_literal_percent_and_utc() -> None:
    result = connector(db_type="mssql", timestamp_column="seq]field")
    query, parameters = result._build_time_filtered_query("SELECT * FROM t WHERE body LIKE '%100%' ORDER BY id", datetime(2026, 1, 1, tzinfo=UTC), "x' OR 1=1 --")
    assert "LIKE '%%100%%'" in query and "[seq]]field] >= %s" in query
    assert parameters == (datetime(2026, 1, 1), "x' OR 1=1 --")


def test_mssql_discovery_fails_closed_and_schema_qualified_queries_remain_intact() -> None:
    from common.data_source.rdbms_connector import rdbms_scope_signature

    result = connector(db_type="mssql", query="")
    with pytest.raises(ConnectorValidationError, match="explicit SELECT"):
        result._base_queries(None)
    query = "SELECT * FROM [other]]schema].[rows]"
    assert connector(db_type="mssql", query=query)._base_queries(None) == [query]
    assert result._identifier("other]schema") == "[other]]schema]"
    assert rdbms_scope_signature({}, "mssql") == rdbms_scope_signature({"port": 1433}, "mssql")


@pytest.mark.parametrize("failure", [None, "connect", "setup", "permission", "object_permission", "cancel"])
def test_mssql_driver_readonly_timeout_redaction_and_setup_cleanup(monkeypatch: pytest.MonkeyPatch, failure: str | None) -> None:
    import pymssql

    calls: list[str] = []
    options: dict[str, Any] = {}
    error = asyncio.CancelledError() if failure == "cancel" else RuntimeError("secret-password")

    class Cursor:
        query = ""

        def execute(self, query: str) -> None:
            self.query = query
            calls.append(query)
            if failure in {"setup", "cancel"}:
                raise error

        def fetchall(self) -> list[tuple[str]]:
            if "sys.objects" in self.query:
                return [("INSERT",)] if failure == "object_permission" else [("SELECT",)]
            return (
                [("CONNECT",), ("SELECT",), ("CONTROL",)]
                if failure == "permission"
                else [("CONNECT",), ("SELECT",), ("VIEW ANY COLUMN MASTER KEY DEFINITION",), ("VIEW ANY COLUMN ENCRYPTION KEY DEFINITION",)]
            )

        def close(self) -> None:
            calls.append("cursor-closed")

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            calls.append("connection-closed")

    def connect(**kwargs: Any) -> Connection:
        options.update(kwargs)
        if failure == "connect":
            raise error
        return Connection()

    monkeypatch.setattr(pymssql, "connect", connect)
    result = connector(db_type="mssql", port=1433)
    result.load_credentials({"username": "readonly", "password": "secret-password"})
    if failure:
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else Exception) as raised:
            result._get_connection()
        if failure == "connect":
            assert "secret-password" not in str(raised.value)
        else:
            assert calls[-2:] == ["cursor-closed", "connection-closed"]
    else:
        connection = result._get_connection()
        assert calls[-1] == "cursor-closed"
        connection.close()
    assert options["login_timeout"] == 10 and options["timeout"] == 30
    assert options["autocommit"] is False and options["port"] == "1433"


def test_mssql_max_cursor_uses_top_and_closes_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[str] = []

    class Cursor:
        def execute(self, query: str) -> None:
            commands.append(query)

        def fetchone(self) -> tuple[int, ...]:
            return (2, 2) if "COUNT" in commands[-1] else (9,)

        def close(self) -> None:
            commands.append("cursor-closed")

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            commands.append("connection-closed")

    result = connector(db_type="mssql", query="SELECT TOP (2) * FROM t WHERE active=1 ORDER BY id")
    monkeypatch.setattr(result, "_get_connection", Connection)
    assert result.get_max_cursor_value() == 9
    assert commands[1].startswith("SELECT TOP (1) [seq]")
    assert "TOP (2)" in commands[1] and "LIMIT" not in commands[1]
    assert commands[-2:] == ["cursor-closed", "connection-closed"]


async def test_mssql_source_is_registered_and_emits_bounded_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from common.constants import FileSource
    from common.data_source.config import DocumentSource

    assert sync.func_factory[FileSource.MSSQL] is sync.MSSQL
    assert FileSource.MSSQL in sync.DELETED_FILE_SYNC_SOURCES
    assert DocumentSource("mssql") is DocumentSource.MSSQL
    config = {"database": "owned", "query": "SELECT * FROM t", "timestamp_column": "seq", "credentials": {"username": "u", "password": "p"}}
    driver = sync.MSSQL(config)
    monkeypatch.setattr(RDBMSConnector, "get_max_cursor_value", lambda self: 12)
    seen: list[tuple[Any, Any]] = []
    monkeypatch.setattr(RDBMSConnector, "load_from_cursor_range", lambda self, start, end: seen.append((start, end)) or iter(()))
    await driver._generate({"kb_id": "kb", "poll_range_start": None})
    assert driver.connector.port == 1433 and seen == [(None, 12)]
    assert driver._completion_state() == {"source": "mssql", "scope": driver.connector.scope_signature, "cursor": 12}


def test_mssql_range_and_full_inventory_escape_all_bound_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[str, tuple[Any, ...]]] = []
    columns = ["id%", "discount%", "category%", "seq%"]

    class Cursor:
        description = [(name,) for name in columns]

        def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> None:
            commands.append((query, parameters))
            if parameters:
                # The pyformat DBAPI must see only the intended range markers.
                rendered = query % parameters
                assert "[discount%]" in rendered and "[seq%]" in rendered
                assert "LIKE '%active%'" in rendered

        def __iter__(self) -> Iterator[tuple[Any, ...]]:
            yield (0, 0, False, 9)

        def close(self) -> None:
            pass

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            pass

    result = connector(
        db_type="mssql",
        query="SELECT TOP (2) * FROM [dbo].[rows] WHERE body LIKE '%active%' ORDER BY [id%]",
        id_column="id%",
        content_columns="discount%",
        metadata_columns="category%",
        timestamp_column="seq%",
    )
    monkeypatch.setattr(result, "_get_connection", Connection)
    documents = [doc for batch in result.load_from_cursor_range(9, 9) for doc in batch]
    assert documents[0].source == "mssql" and documents[0].id.endswith(":0")
    assert documents[0].metadata == {"category%": "False"} and documents[0].blob == "【discount%】:\n0".encode()
    snapshot = [doc for batch in result.retrieve_all_slim_docs_perm_sync() for doc in batch]
    assert [doc.id for doc in snapshot] == [doc.id for doc in documents]
    assert commands[0][1] == (9, 9) and ">= %s" in commands[0][0] and "<= %s" in commands[0][0]
    assert commands[1][1] == () and "TOP (2)" in commands[1][0] and "ORDER BY [id%]" in commands[1][0]
    assert "ragflow_src.[seq%] >=" not in commands[1][0]
