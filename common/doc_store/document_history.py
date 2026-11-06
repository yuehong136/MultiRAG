"""Complete document history snapshots for destructive parse compensation."""

import copy
import json
from collections.abc import Mapping
from typing import Any


def document_index_exists(store: Any, index_name: str, dataset_id: str) -> bool:
    """Only native, failure-preserving probes may confirm an absent index.

    The public index_exist wrappers also return False on transport/auth errors.
    Unhandled backends retain unconditional cleanup rather than infer absence.
    """
    backend = store.db_type()
    if backend in {"elasticsearch", "opensearch"}:
        client = store.es if backend == "elasticsearch" else store.os
        response = client.indices.exists(index=index_name)
        exists = getattr(response, "body", response)
    elif backend == "milvus":
        exists = store._get_connection().has_collection(index_name)
    elif backend == "infinity":
        from infinity.common import InfinityException
        from infinity.errors import ErrorCode

        connection = store.connPool.get_conn()
        try:
            try:
                connection.get_database(store.dbName).get_table(f"{index_name}_{dataset_id}")
            except InfinityException as error:
                if error.error_code == ErrorCode.TABLE_NOT_EXIST:
                    return False
                raise
            return True
        finally:
            store.connPool.release_conn(connection)
    else:
        return True
    if type(exists) is not bool:
        raise RuntimeError("Document index existence could not be confirmed.")
    return exists


def confirm_history_visibility(store: Any, index_name: str) -> None:
    """Refresh mutation/recovery paths that do not promise a wait_for barrier."""
    backend = store.db_type()
    if backend not in {"elasticsearch", "opensearch"}:
        return
    client = store.es if backend == "elasticsearch" else store.os
    response = client.indices.refresh(index=index_name)
    raw = getattr(response, "body", response)
    shards = raw.get("_shards", {}) if isinstance(raw, Mapping) else {}
    total, successful, failed = (shards.get(key) for key in ["total", "successful", "failed"])
    # Unassigned replicas are included in total without being failures. Every
    # active shard must refresh; the subsequent complete search checks primaries.
    if type(total) is not int or type(successful) is not int or type(failed) is not int or failed != 0 or not 0 < successful <= total:
        raise RuntimeError("Document history visibility could not be confirmed.")


def _native_value(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return _native_value(value.tolist())
    if isinstance(value, dict):
        return {key: _native_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_native_value(item) for item in value]
    return value


def document_history(store: Any, index_name: str, dataset_id: str, document_id: str) -> list[dict[str, Any]]:
    """Read complete rows/vectors; never turn an incomplete read into an empty history."""
    backend = store.db_type()
    if backend in {"oceanbase", "seekdb"}:
        from sqlalchemy import column

        table = store.get_table_name(index_name, dataset_id)
        if not store.index_exist(index_name, dataset_id):
            return []
        result = store.client.get(table_name=table, where_clause=[column("doc_id") == document_id, column("kb_id") == dataset_id])
        try:
            return sorted([_native_value(dict(row)) for row in result.mappings()], key=lambda row: row["id"])
        finally:
            result.close()
    if backend == "vastbase":
        from psycopg2 import sql

        connection = store._get_connection()
        try:
            with connection.cursor() as cursor:
                table = f"{index_name}_{dataset_id}"
                if not store._table_exists(cursor, table):
                    return []
                store._register_vector_extension(connection)
                cursor.execute(sql.SQL("SELECT * FROM {}.{} WHERE doc_id = %s AND kb_id = %s").format(sql.Identifier(store.schema), sql.Identifier(table)), (document_id, dataset_id))
                names = [column.name for column in cursor.description]
                return sorted([_native_value(dict(zip(names, row, strict=True))) for row in cursor.fetchall()], key=lambda row: row["id"])
        finally:
            connection.rollback()
            store._release_connection(connection)
    if backend == "milvus":
        from pymilvus import Collection, DataType

        connection = store._get_connection()
        if not connection.has_collection(index_name):
            return []
        store.ensure_collection_loaded(index_name)
        fields = connection.describe_collection(index_name)["fields"]
        vectors = [field["name"] for field in fields if "VECTOR" in DataType(field["type"]).name and not field.get("is_function_output")]
        iterator = Collection(index_name, using=store._using).query_iterator(
            expr=f"doc_id == {json.dumps(document_id)} && kb_id == {json.dumps(dataset_id)}", output_fields=["*", *vectors], batch_size=1000, consistency_level="Strong"
        )
        rows = []
        try:
            while page := iterator.next():
                rows.extend(page)
        finally:
            iterator.close()
        return sorted([_native_value({**copy.deepcopy(row), "id": row.get("id") or row["pk"]}) for row in rows], key=lambda row: row.get("id") or row["pk"])
    if backend == "infinity":
        if not document_index_exists(store, index_name, dataset_id):
            return []
        connection = store.connPool.get_conn()
        try:
            table = connection.get_database(store.dbName).get_table(f"{index_name}_{dataset_id}")
            frame, _ = table.output(["*"]).filter("doc_id = '" + document_id.replace("'", "''") + "'").to_df()
            rows = frame.to_dict(orient="records")
            return sorted([{key: value.tolist() if hasattr(value, "tolist") else value for key, value in row.items()} for row in rows], key=lambda row: row["id"])
        finally:
            store.connPool.release_conn(connection)
    if backend in {"elasticsearch", "opensearch"}:
        if not document_index_exists(store, index_name, dataset_id):
            return []
        client = store.es if backend == "elasticsearch" else store.os
        query = {"bool": {"filter": [{"term": {"doc_id": document_id}}, {"term": {"kb_id": dataset_id}}]}}
        cursor = None
        rows = []
        expected = None
        try:
            response = client.search(
                index=index_name, body={"query": query, "_source": True, "sort": ["_doc"], "size": 1000, "track_total_hits": True}, scroll="1m", allow_partial_search_results=False
            )
            while True:
                raw = getattr(response, "body", response)
                if not isinstance(raw, Mapping):
                    raise RuntimeError("Unknown document history response.")
                cursor = raw.get("_scroll_id") or cursor
                shards = raw.get("_shards", {})
                if raw.get("timed_out") is not False or shards.get("failed") != 0 or shards.get("successful") != shards.get("total") or not shards.get("total"):
                    raise RuntimeError("Incomplete document history response.")
                hits = raw["hits"]
                total = hits["total"]
                total = total.get("value") if isinstance(total, Mapping) and total.get("relation") == "eq" else total
                if type(total) is not int or total < 0 or (expected is not None and expected != total):
                    raise RuntimeError("Unknown document history count.")
                expected = total
                page = hits["hits"]
                if not isinstance(page, list):
                    raise RuntimeError("Unknown document history rows.")
                for hit in page:
                    source = hit.get("_source")
                    if not isinstance(source, Mapping):
                        raise RuntimeError("Missing document history payload.")
                    rows.append({**source, "id": source.get("id", hit["_id"])})
                if len(rows) > expected:
                    raise RuntimeError("Inconsistent document history count.")
                if not page:
                    if len(rows) != expected:
                        raise RuntimeError("Truncated document history.")
                    return sorted(rows, key=lambda row: row["id"])
                if not cursor:
                    raise RuntimeError("Missing document history cursor.")
                response = client.scroll(scroll_id=cursor, scroll="1m")
        finally:
            if cursor:
                client.clear_scroll(scroll_id=cursor)
    raise RuntimeError("Document history snapshots are unavailable for this store.")


def _delete_history_rows(store: Any, condition: dict[str, Any], index_name: str, dataset_id: str) -> Any:
    """Keep native failures visible on destructive, compensating history paths.

    The general connector delete methods can report backend errors as zero
    deletions. Here zero must be an actual acknowledgement, including retries
    on an already empty document.
    """
    backend = store.db_type()
    if backend in {"elasticsearch", "opensearch"}:
        client = store.es if backend == "elasticsearch" else store.os
        filters: list[dict[str, Any]] = [{"term": {"doc_id": condition["doc_id"]}}, {"term": {"kb_id": dataset_id}}]
        if "id" in condition:
            filters.append({"ids": {"values": condition["id"]}})
        response = client.delete_by_query(index=index_name, body={"query": {"bool": {"filter": filters}}}, refresh=True)
        raw = getattr(response, "body", response)
        if not isinstance(raw, Mapping) or raw.get("timed_out") is not False or raw.get("failures") != [] or raw.get("version_conflicts") != 0:
            raise RuntimeError("Document history native deletion failed.")
        return raw.get("deleted")
    if backend == "milvus":
        expression = f"doc_id == {json.dumps(condition['doc_id'])} && kb_id == {json.dumps(dataset_id)}"
        if "id" in condition:
            expression += f" && pk in {json.dumps(condition['id'])}"
        return store._get_connection().delete(index_name, expression=expression).delete_count
    if backend == "infinity":
        connection = store.connPool.get_conn()
        try:
            table = connection.get_database(store.dbName).get_table(f"{index_name}_{dataset_id}")
            result = table.delete(store.equivalent_condition_to_str(condition, table))
            if result.error_code != 0:
                raise RuntimeError("Document history native deletion failed.")
            return result.deleted_rows
        finally:
            store.connPool.release_conn(connection)
    return store.delete(condition, index_name, dataset_id)


def delete_document_history(store: Any, index_name: str, dataset_id: str, document_id: str, *, ids: list[str] | None = None) -> None:
    """Require a typed acknowledgement and verify the exact target is absent."""
    condition: dict[str, Any] = {"doc_id": document_id}
    if ids is not None:
        if not ids:
            return
        condition["id"] = ids
    if not document_index_exists(store, index_name, dataset_id):
        return
    acknowledgement = _delete_history_rows(store, condition, index_name, dataset_id)
    if type(acknowledgement) is not int or acknowledgement < 0:
        raise RuntimeError("Document history deletion failed.")
    # delete_by_query already refreshes the actual connector; a second precise
    # boundary also covers alternate transports and incomplete acknowledgements.
    if store.db_type() in {"elasticsearch", "opensearch"} and document_index_exists(store, index_name, dataset_id):
        confirm_history_visibility(store, index_name)
    remaining = document_history(store, index_name, dataset_id, document_id)
    if remaining if ids is None else any(row.get("id", row.get("pk")) in ids for row in remaining):
        raise RuntimeError("Document history deletion could not be confirmed.")


def restore_document_history(store: Any, index_name: str, dataset_id: str, document_id: str, rows: list[dict[str, Any]], *, ids: list[str] | None = None) -> None:
    """Restore the entire snapshot and independently verify the write wrapper."""
    if ids is not None:
        rows = [row for row in rows if row.get("id", row.get("pk")) in ids]
    delete_document_history(store, index_name, dataset_id, document_id, ids=ids)
    if rows:
        if store.db_type() in {"oceanbase", "seekdb"}:
            # Use raw native rows, preserving extra/JSON/array/vector columns.
            # The public insert intentionally transforms those payloads.
            if store.client.upsert(store.get_table_name(index_name, dataset_id), copy.deepcopy(rows)) is not None:
                raise RuntimeError("Document history native recovery failed.")
        elif store.db_type() == "vastbase":
            _restore_vastbase(store, index_name, dataset_id, rows)
        elif store.db_type() == "infinity":
            connection = store.connPool.get_conn()
            try:
                table = connection.get_database(store.dbName).get_table(f"{index_name}_{dataset_id}")
                result = table.insert(copy.deepcopy(rows))
                if result.error_code != 0:
                    raise RuntimeError("Document history recovery failed.")
            finally:
                store.connPool.release_conn(connection)
        else:
            acknowledgement = store.insert(copy.deepcopy(rows), index_name, dataset_id)
            if not isinstance(acknowledgement, list) or acknowledgement:
                raise RuntimeError("Document history recovery failed.")
        confirm_history_visibility(store, index_name)
    actual = document_history(store, index_name, dataset_id, document_id)
    if ids is not None:
        actual = [row for row in actual if row.get("id", row.get("pk")) in ids]
    if actual != rows:
        raise RuntimeError("Document history recovery could not be confirmed.")


def _restore_vastbase(store: Any, index_name: str, dataset_id: str, rows: list[dict[str, Any]]) -> None:
    from psycopg2 import sql
    from psycopg2.extras import Json, execute_batch

    connection = store._get_connection()
    try:
        store._register_vector_extension(connection)
        with connection.cursor() as cursor:
            columns = list(rows[0])
            statement = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
                sql.Identifier(store.schema), sql.Identifier(f"{index_name}_{dataset_id}"), sql.SQL(",").join(map(sql.Identifier, columns)), sql.SQL(",").join(sql.Placeholder() for _ in columns)
            )
            values = [[Json(row[key]) if isinstance(row[key], dict) else row[key] for key in columns] for row in rows]
            execute_batch(cursor, statement, values)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        store._release_connection(connection)
