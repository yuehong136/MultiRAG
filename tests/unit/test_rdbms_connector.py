import hashlib
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
