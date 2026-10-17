"""Read-only RDBMS rows, typed incremental cursors and complete inventories."""

import base64
import hashlib
import json
import math
import re
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from common.data_source.config import INDEX_BATCH_SIZE, DocumentSource
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError
from common.data_source.interfaces import LoadConnector, PollConnector, SecondsSinceUnixEpoch, SlimConnectorWithPermSync
from common.data_source.models import Document, SlimDocument

_CURSOR_TYPE = "__ragflow_rdbms_cursor_type__"


class DatabaseType(str, Enum):
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, UUID):
        return str(value)
    raise ConnectorValidationError(f"Unsupported RDBMS value type: {type(value).__name__}")


def _text_value(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return json.dumps(_json_value(value), ensure_ascii=False)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=_json_value)
    return str(value)


def rdbms_scope_signature(config: dict[str, Any], db_type: str) -> str:
    """Fingerprint the row/identity contract, excluding credentials rotation/state."""
    scope = {key: config.get(key) or "" for key in ("database", "query", "content_columns", "metadata_columns", "id_column", "timestamp_column")}
    scope.update(
        db_type=db_type, host=config.get("host") or "localhost", port=int(config.get("port") or (3306 if db_type == "mysql" else 5432)), username=(config.get("credentials") or {}).get("username")
    )
    return hashlib.sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class RDBMSConnector(LoadConnector, PollConnector, SlimConnectorWithPermSync):
    def __init__(
        self,
        db_type: str,
        host: str,
        port: int,
        database: str,
        query: str,
        content_columns: str,
        metadata_columns: str | None = None,
        id_column: str | None = None,
        timestamp_column: str | None = None,
        batch_size: int = INDEX_BATCH_SIZE,
    ) -> None:
        self.db_type = DatabaseType(db_type.lower())
        self.host, self.port, self.database = host.strip(), int(port), database.strip()
        self.query = query.strip().rstrip(";").strip()
        self.content_columns = [col.strip() for col in (content_columns or "").split(",") if col.strip()]
        self.metadata_columns = [col.strip() for col in (metadata_columns or "").split(",") if col.strip()]
        self.id_column = id_column.strip() if id_column else None
        self.timestamp_column = timestamp_column.strip() if timestamp_column else None
        self.batch_size = int(batch_size)
        if self.batch_size <= 0:
            raise ConnectorValidationError("RDBMS requires a positive batch size")
        self._credentials: dict[str, Any] = {}
        self.pending_cursor: Any = None
        self.scope_signature: str | None = None

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        if not credentials.get("username") or not credentials.get("password"):
            raise ConnectorMissingCredentialError("RDBMS requires username and password")
        self._credentials = dict(credentials)
        return None

    def _get_connection(self) -> Any:
        if not self._credentials or not self.host or not self.database:
            raise ConnectorMissingCredentialError("RDBMS connection settings are incomplete")
        if self.db_type == DatabaseType.MYSQL:
            import mysql.connector

            connection = mysql.connector.connect(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self._credentials["username"],
                password=self._credentials["password"],
                charset="utf8mb4",
                time_zone="+00:00",
                connection_timeout=10,
                read_timeout=30,
                write_timeout=30,
            )
            try:
                connection.start_transaction(isolation_level="REPEATABLE READ", readonly=True)
            except Exception:
                connection.close()
                raise
            return connection
        import psycopg2
        from psycopg2.extras import register_uuid

        connection = psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self._credentials["username"],
            password=self._credentials["password"],
            connect_timeout=10,
            options="-c timezone=UTC -c statement_timeout=30000",
        )
        try:
            register_uuid(conn_or_curs=connection)
            connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
        except Exception:
            connection.close()
            raise
        return connection

    @contextmanager
    def _read(self) -> Iterator[tuple[Any, Any]]:
        connection = self._get_connection()
        try:
            cursor = connection.cursor()
            try:
                yield connection, cursor
            finally:
                cursor.close()
        finally:
            connection.close()

    def _identifier(self, value: str) -> str:
        quote = "`" if self.db_type == DatabaseType.MYSQL else '"'
        return quote + value.replace(quote, quote * 2) + quote

    def _base_queries(self, cursor: Any) -> list[str]:
        if self.query:
            return [self.query]
        if self.db_type == DatabaseType.MYSQL:
            # SHOW TABLES hides inaccessible tables. Only a schema-wide SELECT
            # grant can make discovery authoritative; explicit queries need none.
            cursor.execute("SHOW GRANTS FOR CURRENT_USER")
            grants = [row[0] for row in cursor.fetchall()]
            permitted = False
            for grant in grants:
                if grant.upper().startswith("REVOKE "):
                    raise ConnectorValidationError("MySQL discovery cannot trust partial revokes; configure an explicit query")
                match = re.match(r"GRANT (.+?) ON (.+?) TO ", grant, flags=re.IGNORECASE)
                if match and match[2] in {"*.*", self._identifier(self.database) + ".*"} and {part.strip().upper() for part in match[1].split(",")} & {"SELECT", "ALL PRIVILEGES"}:
                    permitted = True
            if not permitted:
                raise ConnectorValidationError("MySQL discovery requires database-wide SELECT; configure an explicit query")
            cursor.execute("SHOW TABLES")
        else:
            # pg_catalog retains inaccessible tables; SELECT then fails closed.
            cursor.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' ORDER BY tablename")
        tables = cursor.fetchall()
        return [f"SELECT * FROM {self._identifier(row[0])}" for row in tables]

    def _wrap_query(self, base: str, projection: str = "*") -> str:
        return f"SELECT {projection} FROM ({base}) AS ragflow_src"

    @staticmethod
    def serialize_cursor_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ConnectorValidationError("Non-finite RDBMS cursor")
            return value
        if isinstance(value, datetime):
            return {_CURSOR_TYPE: "datetime", "value": _utc(value).isoformat()}
        if isinstance(value, date):
            return {_CURSOR_TYPE: "date", "value": value.isoformat()}
        if isinstance(value, Decimal) and value.is_finite():
            return {_CURSOR_TYPE: "decimal", "value": str(value)}
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {_CURSOR_TYPE: "bytes", "value": base64.b64encode(bytes(value)).decode("ascii")}
        if isinstance(value, UUID):
            return {_CURSOR_TYPE: "uuid", "value": str(value)}
        raise ConnectorValidationError(f"Unsupported RDBMS cursor: {type(value).__name__}")

    @staticmethod
    def deserialize_cursor_value(value: Any) -> Any:
        if not isinstance(value, dict):
            RDBMSConnector.serialize_cursor_value(value)
            return value
        if set(value) != {_CURSOR_TYPE, "value"} or not isinstance(value["value"], str):
            raise ConnectorValidationError("Invalid serialized RDBMS cursor")
        converters = {
            "datetime": lambda item: _utc(datetime.fromisoformat(item)),
            "date": date.fromisoformat,
            "decimal": Decimal,
            "bytes": lambda item: base64.b64decode(item, validate=True),
            "uuid": UUID,
        }
        try:
            result = converters[value[_CURSOR_TYPE]](value["value"])
            RDBMSConnector.serialize_cursor_value(result)
            return result
        except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
            raise ConnectorValidationError("Invalid serialized RDBMS cursor") from exc

    def _build_time_filtered_query(self, base: str, start: Any = None, end: Any = None) -> tuple[str, tuple[Any, ...]]:
        query = self._wrap_query(base)
        if not self.timestamp_column:
            return query, ()
        conditions, parameters = [], []
        # Frozen follow-up 5fc254eb2 replays the boundary to cover equal cursors.
        for value, operator in ((start, ">="), (end, "<=")):
            if value is not None:
                self.serialize_cursor_value(value)
                conditions.append(f"ragflow_src.{self._identifier(self.timestamp_column)} {operator} %s")
                bound = _utc(value) if isinstance(value, datetime) else value
                if self.db_type == DatabaseType.MYSQL and isinstance(bound, datetime):
                    bound = bound.replace(tzinfo=None)
                parameters.append(bound)
        return query + (" WHERE " + " AND ".join(conditions) if conditions else ""), tuple(parameters)

    def _content_columns_for_row(self, row: dict[str, Any]) -> list[str]:
        return self.content_columns or [column for column in row if column not in {self.id_column, self.timestamp_column}]

    def _content(self, row: dict[str, Any]) -> str:
        parts = []
        for column in self._content_columns_for_row(row):
            value = row[column]
            if value is not None:
                parts.append(f"【{column}】:\n{_text_value(value)}")
        return "\n\n".join(parts)

    def _document_id(self, row: dict[str, Any], content: str) -> str:
        identifier = row.get(self.id_column) if self.id_column else None
        if identifier is None:
            identifier = hashlib.md5(content.encode()).hexdigest()
        elif isinstance(identifier, (bytes, bytearray, memoryview)):
            identifier = "base64:" + base64.b64encode(bytes(identifier)).decode("ascii")
        elif isinstance(identifier, (dict, list)):
            identifier = json.dumps(identifier, sort_keys=True, ensure_ascii=False, default=_json_value)
        # Preserve the pre-port Enum rendering used by existing external IDs.
        return f"{self.db_type}:{self.database}:{identifier}"

    def _metadata(self, row: dict[str, Any]) -> dict[str, str]:
        return {column: (value.isoformat() if isinstance(value, (datetime, date)) else _text_value(value)) for column in self.metadata_columns if (value := row[column]) is not None}

    def _rows(self, start: Any = None, end: Any = None) -> Iterator[dict[str, Any]]:
        seen: dict[str, tuple[int, str]] = {}
        with self._read() as (_, cursor):
            bases = self._base_queries(cursor)
            for index, base in enumerate(bases):
                query, parameters = self._build_time_filtered_query(base, start, end)
                if self.content_columns:
                    selected = list(dict.fromkeys([*self.content_columns, *self.metadata_columns, *(column for column in (self.id_column, self.timestamp_column) if column)]))
                    projection = ", ".join("ragflow_src." + self._identifier(column) for column in selected)
                    query = query.replace("SELECT * FROM (", f"SELECT {projection} FROM (", 1)
                cursor.execute(query, parameters)
                if cursor.description is None:
                    raise ConnectorValidationError("RDBMS query did not return rows")
                columns = [column[0] for column in cursor.description]
                required = {*self.content_columns, *self.metadata_columns, *(column for column in (self.id_column, self.timestamp_column) if column)}
                if len(set(columns)) != len(columns) or not required.issubset(columns):
                    raise ConnectorValidationError("RDBMS query has duplicate/missing configured columns")
                while batch := cursor.fetchmany(self.batch_size):
                    for values in batch:
                        row = dict(zip(columns, values, strict=True)) if not isinstance(values, dict) else values
                        if self.timestamp_column:
                            value = row[self.timestamp_column]
                            if value is None:
                                raise ConnectorValidationError("RDBMS cursor column cannot contain NULL")
                            self.serialize_cursor_value(value)
                        content = self._content(row)
                        if not content:
                            continue
                        identifier = self._document_id(row, content)
                        fingerprint = hashlib.sha256(json.dumps(row, ensure_ascii=False, default=_json_value, sort_keys=True).encode()).hexdigest()
                        if identifier in seen:
                            if seen[identifier] != (index, fingerprint):
                                raise ConnectorValidationError("RDBMS document ID collision; use a query with unique IDs")
                            continue
                        seen[identifier] = index, fingerprint
                        yield row

    def _row_to_document(self, row: dict[str, Any]) -> Document:
        content = self._content(row)
        updated = row.get(self.timestamp_column) if self.timestamp_column else None
        title_columns = self._content_columns_for_row(row)
        title = _text_value(row[title_columns[0]]).replace("\n", " ").replace("\r", " ").strip()[:100] or "database_record"
        return Document(
            id=self._document_id(row, content),
            blob=content.encode(),
            source=DocumentSource(self.db_type.value),
            semantic_identifier=title,
            extension=".txt",
            doc_updated_at=_utc(updated) if isinstance(updated, datetime) else datetime.now(UTC),
            size_bytes=len(content.encode()),
            metadata=self._metadata(row) or None,
        )

    def _yield_documents(self, start: Any = None, end: Any = None) -> Generator[list[Document], None, None]:
        batch: list[Document] = []
        for row in self._rows(start, end):
            batch.append(self._row_to_document(row))
            if len(batch) >= self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def get_max_cursor_value(self) -> Any:
        if not self.timestamp_column:
            return None
        result = None
        with self._read() as (_, cursor):
            for base in self._base_queries(cursor):
                field = self._identifier(self.timestamp_column)
                cursor.execute(self._wrap_query(base, f"COUNT(*), COUNT({field})"))
                total, valid = cursor.fetchone()
                if total != valid:
                    raise ConnectorValidationError("RDBMS cursor column cannot contain NULL")
                cursor.execute(self._wrap_query(base, field) + f" WHERE {field} IS NOT NULL ORDER BY {field} DESC LIMIT 1")
                row = cursor.fetchone()
                if row is None:
                    continue
                candidate = self.deserialize_cursor_value(self.serialize_cursor_value(row[0]))
                if result is None or candidate > result:
                    result = candidate
        return result

    def load_from_cursor_range(self, start_value: Any = None, end_value: Any = None) -> Generator[list[Document], None, None]:
        if end_value is None:
            return iter(())
        # A reset/decreasing source cursor needs a full bounded replay.
        # Text ordering belongs to the database collation, not Python. A full
        # bounded replay avoids guessing that ordering or skipping reset rows.
        if isinstance(start_value, str) or (start_value is not None and end_value < start_value):
            start_value = None
        return self._yield_documents(start_value, end_value)

    def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Generator[list[SlimDocument], None, None]:
        del callback
        batch: list[SlimDocument] = []
        # Full configured query, including its WHERE/ORDER/LIMIT, never the
        # incremental cursor window. Read all eligibility fields but build no blob.
        for row in self._rows():
            batch.append(SlimDocument(id=self._document_id(row, self._content(row))))
            if len(batch) >= self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def load_from_state(self) -> Generator[list[Document], None, None]:
        return self._yield_documents()

    def poll_source(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> Generator[list[Document], None, None]:
        return self._yield_documents(start, end)

    def validate_connector_settings(self) -> None:
        with self._read() as (_, cursor):
            cursor.execute("SELECT 1")
            cursor.fetchone()
