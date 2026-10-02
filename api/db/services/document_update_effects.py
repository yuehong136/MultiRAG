"""Exact side effects included in the document ingest recovery journal."""

import copy
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import String, delete, literal_column, select, update
from sqlalchemy.orm import Session

from api.db.db_models import Document, DocumentMetadata, File, File2Document, Knowledgebase
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_image_lock import image_write_locks, pending_task_image_references
from api.db.services.metadata_store_sql import SqlMetadataStore
from common import settings
from common.doc_store.document_history import confirm_history_visibility, document_history, restore_document_history
from core.nlp import rag_tokenizer, search


def _row(model: type[Any], row: Any) -> dict[str, Any]:
    return {column.name: copy.deepcopy(getattr(row, column.name)) for column in model.__table__.columns}


def sql_row_version(db: Session, model: type[Any], identifier: str) -> str:
    """PostgreSQL ownership includes later writes assigning the same value."""
    value = db.scalar(select(literal_column("xmin").cast(String)).select_from(model.__table__).where(model.id == identifier))
    if not isinstance(value, str):
        raise RuntimeError("Document write version is unavailable.")
    return value


def _strict_bytes(bucket: str, key: str) -> bytes | None:
    storage = settings.STORAGE_IMPL
    reader = getattr(storage, "get_bytes", None)
    underlying = getattr(storage, "storage_impl", storage)
    if not callable(reader) or not callable(getattr(underlying, "get_bytes", None)):
        raise RuntimeError("Strict image storage reads are unavailable.")
    value = reader(bucket, key)
    if value is not None and not isinstance(value, bytes):
        raise RuntimeError("Invalid image storage response.")
    return value


def _raw_bytes(bucket: str, key: str) -> bytes | None:
    storage = settings.STORAGE_IMPL
    underlying = getattr(storage, "storage_impl", storage)
    content = underlying.get_bytes(bucket, key)
    if content is not None and not isinstance(content, bytes):
        raise RuntimeError("Invalid native image storage response.")
    return content


def _protected_images(db: Session, doc: Document, candidates: set[str]) -> set[str]:
    """Read complete references, including disabled rows and original sources."""
    protected: set[str] = pending_task_image_references(db, doc.id, candidates)
    for file in db.scalars(select(File)):
        if file.location:
            protected.add(f"{file.parent_id}-{file.location}")
    datasets = {kb.id: kb for kb in db.scalars(select(Knowledgebase))}
    kb = datasets.get(doc.kb_id)
    if kb is None:
        raise RuntimeError("Image reference dataset is unavailable.")
    for dataset in datasets.values():
        protected.update(_other_image_references(dataset, doc.id if dataset.id == doc.kb_id else None, candidates))
    for other in db.scalars(select(Document)):
        if other.location:
            protected.add(f"{other.kb_id}-{other.location}")
        if other.thumbnail and not other.thumbnail.startswith("data:"):
            protected.add(f"{other.kb_id}-{other.thumbnail}")
        if other.id == doc.id:
            continue
        kb = datasets.get(other.kb_id)
        if kb is None:
            raise RuntimeError("Image reference dataset is unavailable.")
        for row in document_history(settings.docStoreConn, search.index_name_one(kb.tenant_id, kb.name), kb.id, other.id):
            if row.get("img_id") in candidates:
                protected.add(row["img_id"])
    return protected


def _other_image_references(kb: Knowledgebase, document_id: str | None, candidates: set[str]) -> set[str]:
    """Complete native read also protects chunks with no surviving SQL document."""
    store = settings.docStoreConn
    index = search.index_name_one(kb.tenant_id, kb.name)
    if not candidates or not store.index_exist(index, kb.id):
        return set()
    backend = store.db_type()
    rows: list[Any]
    if backend == "milvus":
        from pymilvus import Collection

        iterator = Collection(index, using=store._using).query_iterator(
            expr=f"img_id in {json.dumps(sorted(candidates))}" + (f" && doc_id != {json.dumps(document_id)}" if document_id is not None else ""),
            output_fields=["img_id", "doc_id"],
            batch_size=1000,
            consistency_level="Strong",
        )
        rows = []
        try:
            while page := iterator.next():
                rows.extend(page)
        finally:
            iterator.close()
    elif backend in {"elasticsearch", "opensearch"}:
        client = store.es if backend == "elasticsearch" else store.os
        response = client.search(
            index=index,
            body={
                "query": {"bool": {"filter": [{"terms": {"img_id": sorted(candidates)}}], "must_not": [{"term": {"doc_id": document_id}}] if document_id is not None else []}},
                "_source": ["img_id", "doc_id"],
                "size": 1000,
                "sort": ["_doc"],
                "track_total_hits": True,
            },
            scroll="1m",
            allow_partial_search_results=False,
        )
        rows, cursor, expected = [], None, None
        try:
            while True:
                raw = getattr(response, "body", response)
                cursor = raw.get("_scroll_id") or cursor
                shards = raw.get("_shards", {})
                total = raw.get("hits", {}).get("total")
                total = total.get("value") if isinstance(total, dict) and total.get("relation") == "eq" else total
                if (
                    raw.get("timed_out") is not False
                    or shards.get("failed") != 0
                    or not shards.get("total")
                    or shards.get("successful") != shards["total"]
                    or type(total) is not int
                    or (expected is not None and expected != total)
                ):
                    raise RuntimeError("Image reference read was incomplete.")
                expected = total
                page = raw["hits"]["hits"]
                rows.extend(hit["_source"] for hit in page)
                if not page:
                    if len(rows) != total:
                        raise RuntimeError("Image reference read was truncated.")
                    break
                if not cursor or len(rows) > total:
                    raise RuntimeError("Image reference cursor is unavailable.")
                response = client.scroll(scroll_id=cursor, scroll="1m")
        finally:
            if cursor:
                client.clear_scroll(scroll_id=cursor)
    elif backend in {"oceanbase", "seekdb"}:
        from sqlalchemy import column

        result = store.client.get(
            table_name=store.get_table_name(index, kb.id), where_clause=[column("img_id").in_(candidates), *([column("doc_id") != document_id] if document_id is not None else [])]
        )
        try:
            rows = [dict(row) for row in result.mappings()]
        finally:
            result.close()
    elif backend == "infinity":
        connection = store.connPool.get_conn()
        try:
            table = connection.get_database(store.dbName).get_table(f"{index}_{kb.id}")
            images = " OR ".join("img_id = '" + value.replace("'", "''") + "'" for value in sorted(candidates))
            condition = ("doc_id != '" + document_id.replace("'", "''") + "' AND " if document_id is not None else "") + "(" + images + ")"
            frame, _ = table.output(["img_id", "doc_id"]).filter(condition).to_df()
            rows = frame.to_dict(orient="records")
        finally:
            store.connPool.release_conn(connection)
    elif backend == "vastbase":
        from psycopg2 import sql

        connection = store._get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SELECT img_id, doc_id FROM {}.{} WHERE img_id = ANY(%s)" + (" AND doc_id <> %s" if document_id is not None else "")).format(
                        sql.Identifier(store.schema), sql.Identifier(f"{index}_{kb.id}")
                    ),
                    (sorted(candidates), *([document_id] if document_id is not None else [])),
                )
                rows = [{"img_id": row[0], "doc_id": row[1]} for row in cursor.fetchall()]
        finally:
            connection.rollback()
            store._release_connection(connection)
    else:
        raise RuntimeError("Complete image reference reads are unavailable.")
    if any(not isinstance(row, dict) or row.get("img_id") not in candidates or (document_id is not None and row.get("doc_id") == document_id) for row in rows):
        raise RuntimeError("Invalid image reference result.")
    return {row["img_id"] for row in rows}


def prepare_update_effects(db: Session, doc: Document, kb: Knowledgebase, values: dict[str, Any], reset: bool, metadata: dict[str, Any] | None, principal_id: str) -> dict[str, Any]:
    """Capture every affected byte and SQL row before the first mutation."""
    data: dict[str, Any] = {"files": [], "objects": [], "metadata": None, "index_values": {}, "change_status": "status" in values}
    if "name" in values:
        links = list(db.scalars(select(File2Document).where(File2Document.document_id == doc.id).order_by(File2Document.id).with_for_update()))
        for identifier in sorted({link.file_id for link in links if link.file_id}):
            file = db.scalar(select(File).where(File.id == identifier).with_for_update())
            if file is None:
                raise RuntimeError("Document source file is unavailable.")
            refs = list(db.scalars(select(File2Document.document_id).where(File2Document.file_id == identifier)))
            # A shared source retains its name for every other document.
            if file.tenant_id not in {kb.tenant_id, principal_id} or any(reference != doc.id for reference in refs):
                continue
            original = _row(File, file)
            data["files"].append({"original": original, "original_version": sql_row_version(db, File, file.id), "applied": {**copy.deepcopy(original), "name": values["name"]}})
        title = rag_tokenizer.tokenize(values["name"])
        data["index_values"] = {"docnm_kwd": values["name"], "title_tks": title, "title_sm_tks": rag_tokenizer.fine_grained_tokenize(title)}
    if metadata is not None:
        processed = DocMetadataService._split_combined_values(copy.deepcopy(metadata))
        if settings.DOC_ENGINE.lower() == "milvus":
            row = db.scalar(select(DocumentMetadata).where(DocumentMetadata.id == doc.id).with_for_update())
            data["metadata"] = {"kind": "sql", "original": _row(DocumentMetadata, row) if row else None, "value": processed, "applied": None}
        else:
            index = DocMetadataService._get_doc_meta_index_name(kb.tenant_id)
            original = settings.docStoreConn.get(doc.id, index, [kb.id]) if settings.docStoreConn.index_exist(index, "") else None
            if original is not None and not isinstance(original, dict):
                raise RuntimeError("Document metadata snapshot is unavailable.")
            data["metadata"] = {"kind": "engine", "index": index, "original": copy.deepcopy(original), "value": processed}
    if reset:
        rows = document_history(settings.docStoreConn, search.index_name_one(kb.tenant_id, kb.name), kb.id, doc.id)
        candidates = {row["img_id"] for row in rows if isinstance(row.get("img_id"), str) and row["img_id"]}
        protected = _protected_images(db, doc, candidates) if candidates else set()
        for identifier in sorted(candidates - protected):
            bucket, separator, key = identifier.partition("-")
            if not separator or bucket != kb.id or not key or len(key) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in key):
                continue
            content = _strict_bytes(bucket, key)
            # A strict adapter distinguishes a known missing object from failure.
            if content is not None:
                raw = _raw_bytes(bucket, key)
                if raw is None:
                    raise RuntimeError("Document image snapshot changed.")
                data["objects"].append({"bucket": bucket, "key": key, "bytes": content, "raw_bytes": raw, "delete_attempted": False})
    return data


def apply_update_sql(db: Session, doc: Document, kb: Knowledgebase, data: dict[str, Any]) -> None:
    for file in data["files"]:
        result = db.execute(update(File).where(File.id == file["original"]["id"]).values(name=file["applied"]["name"]))
        if result.rowcount != 1:
            raise RuntimeError("Document source rename could not be confirmed.")
        file["applied_version"] = sql_row_version(db, File, file["original"]["id"])
    metadata = data["metadata"]
    if metadata and metadata["kind"] == "sql":
        SqlMetadataStore.upsert_in_transaction(db, doc.id, kb.tenant_id, kb.id, metadata["value"])
        metadata["applied"] = copy.deepcopy(dict(db.execute(select(DocumentMetadata.__table__).where(DocumentMetadata.id == doc.id)).mappings().one()))
        metadata["applied_version"] = sql_row_version(db, DocumentMetadata, doc.id)


def apply_update_store(db: Session, doc: Document, kb: Knowledgebase, data: dict[str, Any], rows: list[dict[str, Any]], status: str, on_record: Callable[[], None] | None = None) -> None:
    from api.db.services.document_ingest_service import _history_for_status

    store = settings.docStoreConn
    index = search.index_name_one(kb.tenant_id, kb.name)
    expected = _history_for_status(rows, status) if data["change_status"] else copy.deepcopy(rows)
    if rows:
        groups: dict[str, dict[str, Any]] = {}
        for row in expected:
            values = copy.deepcopy(data["index_values"])
            if data["change_status"]:
                values["available_int"] = row["available_int"]
            if values:
                groups[row["id"]] = values
                row.update(values)
        if data["index_values"]:
            # Public rename adapters may rewrite creation times or omit native
            # vectors. Replace only this document from its complete snapshot.
            restore_document_history(store, index, kb.id, doc.id, expected)
        else:
            for identifier, values in groups.items():
                acknowledgement = store.update({"doc_id": doc.id, "id": identifier}, values, index, kb.id)
                if acknowledgement is not True:
                    raise RuntimeError("Document index update failed.")
        if groups:
            confirm_history_visibility(store, index)
            if document_history(store, index, kb.id, doc.id) != expected:
                raise RuntimeError("Document index update could not be confirmed.")
    metadata = data["metadata"]
    if metadata and metadata["kind"] == "engine":
        if DocMetadataService._store().upsert(db, doc.id, kb.tenant_id, kb.id, metadata["value"]) is not True:
            raise RuntimeError("Document metadata write failed.")
        actual = store.get(doc.id, metadata["index"], [kb.id])
        if not isinstance(actual, dict) or actual.get("meta_fields") != metadata["value"]:
            raise RuntimeError("Document metadata write could not be confirmed.")
    _delete_update_images(db, doc, data["objects"], on_record)


def _delete_update_images(db: Session, doc: Document, objects: list[dict[str, Any]], on_record: Callable[[], None] | None) -> None:
    with image_write_locks(db.get_bind(), [(item["bucket"], item["key"]) for item in objects]):
        _delete_locked_images(db, doc, objects, on_record)


def _delete_locked_images(db: Session, doc: Document, objects: list[dict[str, Any]], on_record: Callable[[], None] | None) -> None:
    protected = _protected_images(db, doc, {f"{item['bucket']}-{item['key']}" for item in objects}) if objects else set()
    for item in objects:
        if f"{item['bucket']}-{item['key']}" in protected:
            continue
        if _strict_bytes(item["bucket"], item["key"]) != item["bytes"] or _raw_bytes(item["bucket"], item["key"]) != item["raw_bytes"]:
            raise RuntimeError("Document image changed before deletion.")
        item["delete_attempted"] = True
        if on_record is not None:
            on_record()
        acknowledgement = settings.STORAGE_IMPL.rm(item["bucket"], item["key"])
        if acknowledgement is False or _strict_bytes(item["bucket"], item["key"]) is not None:
            raise RuntimeError("Document image deletion could not be confirmed.")


def restore_update_effects(db: Session, doc: Document, kb: Knowledgebase, data: dict[str, Any], *, owned_sql: bool) -> None:
    """Compensate exact owned effects; never overwrite a later object/SQL winner."""
    with image_write_locks(db.get_bind(), [(item["bucket"], item["key"]) for item in data["objects"]]):
        _restore_locked_images(data["objects"])
    _restore_update_sql(db, doc, kb, data, owned_sql=owned_sql)


def _restore_locked_images(objects: list[dict[str, Any]]) -> None:
    for item in objects:
        if not item.get("delete_attempted"):
            continue
        current = _strict_bytes(item["bucket"], item["key"])
        raw = _raw_bytes(item["bucket"], item["key"])
        if current == item["bytes"] and raw == item["raw_bytes"]:
            continue
        if current is not None or raw is not None:
            # A later independently written object owns its bytes. Its presence
            # settles our deletion attempt without replacing that writer.
            continue
        storage = settings.STORAGE_IMPL
        underlying = getattr(storage, "storage_impl", storage)
        acknowledgement = underlying.put(item["bucket"], item["key"], item["raw_bytes"])
        if acknowledgement is False or _strict_bytes(item["bucket"], item["key"]) != item["bytes"] or _raw_bytes(item["bucket"], item["key"]) != item["raw_bytes"]:
            raise RuntimeError("Document image restoration could not be confirmed.")


def _restore_update_sql(db: Session, doc: Document, kb: Knowledgebase, data: dict[str, Any], *, owned_sql: bool) -> None:
    for file in data["files"]:
        current = db.scalar(select(File).where(File.id == file["original"]["id"]).with_for_update().execution_options(populate_existing=True))
        if current is None:
            continue
        actual = _row(File, current)
        if actual == file["original"]:
            continue
        if not owned_sql or actual != file["applied"] or sql_row_version(db, File, current.id) != file.get("applied_version"):
            continue
        result = db.execute(update(File).where(File.id == current.id).values(**file["original"]))
        if result.rowcount != 1:
            raise RuntimeError("Document source recovery could not be confirmed.")
    metadata = data["metadata"]
    if metadata and metadata["kind"] == "sql":
        actual = db.execute(select(DocumentMetadata.__table__).where(DocumentMetadata.id == doc.id).with_for_update()).mappings().one_or_none()
        current = dict(actual) if actual is not None else None
        if current != metadata["original"]:
            if not owned_sql or current != metadata["applied"] or sql_row_version(db, DocumentMetadata, doc.id) != metadata.get("applied_version"):
                return
            db.execute(delete(DocumentMetadata).where(DocumentMetadata.id == doc.id))
            if metadata["original"] is not None:
                db.execute(DocumentMetadata.__table__.insert(), metadata["original"])
    elif metadata:
        store, index = settings.docStoreConn, metadata["index"]
        current = store.get(doc.id, index, [kb.id]) if store.index_exist(index, "") else None
        if current != metadata["original"]:
            if not isinstance(current, dict) or current.get("meta_fields") != metadata["value"]:
                return
            acknowledgement = store.delete({"id": doc.id}, index, kb.id)
            if type(acknowledgement) is not int or acknowledgement < 0:
                raise RuntimeError("Document metadata recovery deletion failed.")
            if metadata["original"] is not None:
                acknowledgement = store.insert([copy.deepcopy(metadata["original"])], index, kb.id)
                if not isinstance(acknowledgement, list) or acknowledgement:
                    raise RuntimeError("Document metadata recovery failed.")
            confirm_history_visibility(store, index)
            if store.get(doc.id, index, [kb.id]) != metadata["original"]:
                raise RuntimeError("Document metadata recovery could not be confirmed.")
