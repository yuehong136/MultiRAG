#
#  Copyright 2026 The MultiRAG Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""
Document Metadata Service

Unified business entry-point for document metadata.
All callers MUST go through this service — never touch the store directly.

Storage is delegated to MetadataStore implementations:
- Milvus backend  → SqlMetadataStore   (independent SQL table)
- ES/Infinity     → EngineMetadataStore (docStoreConn sidecar index)
"""

import json
import logging
import re
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase
from api.db.services.metadata_store import MetadataStore
from common import settings
from common.metadata_utils import dedupe_list

logger = logging.getLogger(__name__)

# ── singleton store instances ──────────────────────────────────────────────────

_sql_store: MetadataStore | None = None
_engine_store: MetadataStore | None = None


def _get_sql_store() -> MetadataStore:
    global _sql_store
    if _sql_store is None:
        from api.db.services.metadata_store_sql import SqlMetadataStore

        _sql_store = SqlMetadataStore()
    return _sql_store


def _get_engine_store() -> MetadataStore:
    global _engine_store
    if _engine_store is None:
        from api.db.services.metadata_store_engine import EngineMetadataStore

        _engine_store = EngineMetadataStore()
    return _engine_store


class DocMetadataService:
    """Unified service for managing document metadata."""

    # ── store selection ────────────────────────────────────────────────────────

    @staticmethod
    def _store() -> MetadataStore:
        if settings.DOC_ENGINE.lower() == "milvus":
            return _get_sql_store()
        return _get_engine_store()

    # ── index naming (kept for backward-compat references) ─────────────────────

    @staticmethod
    def _get_doc_meta_index_name(tenant_id: str) -> str:
        return f"multirag_doc_meta_{tenant_id}"

    # ── tenant / kb resolution ─────────────────────────────────────────────────

    @classmethod
    def _get_tenant_kb_for_doc(cls, db: Session, doc_id: str) -> tuple[str | None, str | None]:
        row = db.execute(select(Document.kb_id, Knowledgebase.tenant_id).join(Knowledgebase, Knowledgebase.id == Document.kb_id).where(Document.id == doc_id)).first()
        if not row:
            return None, None
        return row.tenant_id, row.kb_id

    @classmethod
    def _kb_tenant(cls, db: Session, kb_id: str) -> str | None:
        row = db.execute(select(Knowledgebase.tenant_id).where(Knowledgebase.id == kb_id)).first()
        return row.tenant_id if row else None

    # ── split combined values ──────────────────────────────────────────────────

    @classmethod
    def _split_combined_values(cls, meta_fields: dict) -> dict:
        """Split combined string values (e.g. '关羽、孙权' → ['关羽', '孙权'])."""
        if not meta_fields or not isinstance(meta_fields, dict):
            return meta_fields
        processed = {}
        for key, value in meta_fields.items():
            if isinstance(value, list):
                new_values = []
                for item in value:
                    if isinstance(item, str):
                        parts = re.split(r"[、,，;；|]+", item.strip())
                        parts = [p.strip() for p in parts if p.strip()]
                        new_values.extend(parts or [item])
                    else:
                        new_values.append(item)
                processed[key] = dedupe_list(new_values)
            else:
                processed[key] = value
        return processed

    # ── public CRUD ────────────────────────────────────────────────────────────

    @classmethod
    def insert_document_metadata(cls, db: Session, doc_id: str, meta_fields: dict) -> bool:
        tenant_id, kb_id = cls._get_tenant_kb_for_doc(db, doc_id)
        if not tenant_id:
            logger.warning("Doc %s not found for metadata insertion", doc_id)
            return False
        meta_fields = cls._split_combined_values(meta_fields) or {}
        return cls._store().upsert(db, doc_id, tenant_id, kb_id, meta_fields)

    @classmethod
    def update_document_metadata(cls, db: Session, doc_id: str, meta_fields: dict) -> bool:
        tenant_id, kb_id = cls._get_tenant_kb_for_doc(db, doc_id)
        if not tenant_id:
            logger.warning("Doc %s not found for metadata update", doc_id)
            return False
        processed = cls._split_combined_values(meta_fields) or {}
        return cls._store().upsert(db, doc_id, tenant_id, kb_id, processed)

    @classmethod
    def delete_document_metadata(cls, db: Session, doc_id: str, kb_id: str, tenant_id: str | None = None) -> bool:
        # Get tenant_id from kb_id if not provided
        if tenant_id is None:
            tenant_id = cls._kb_tenant(db, kb_id)
            if not tenant_id:
                logger.warning("Knowledgebase %s not found for metadata deletion", kb_id)
                return False
        return cls._store().delete(db, doc_id, tenant_id, kb_id)

    @classmethod
    def get_document_metadata(cls, db: Session, doc_id: str) -> dict:
        tenant_id, kb_id = cls._get_tenant_kb_for_doc(db, doc_id)
        if not tenant_id:
            return {}
        return cls._store().get(db, doc_id, tenant_id, kb_id)

    @classmethod
    def delete_tenant_metadata_container(cls, tenant_id: str) -> bool:
        """Delete the tenant-scoped metadata container when the backend uses one."""
        return cls._store().delete_tenant_container(tenant_id)

    # ── batch read for N+1 avoidance ───────────────────────────────────────────

    @classmethod
    def get_metadata_for_documents(cls, db: Session, doc_ids: list[str] | None, kb_id: str) -> dict[str, dict]:
        """Return {doc_id: meta_fields} for a batch of documents.

        Avoids N+1 queries when populating document list responses.
        """
        store = cls._store()
        if doc_ids is None:
            tenant_id = cls._kb_tenant(db, kb_id)
            if not tenant_id:
                return {}
            return dict(store.list_by_kb_ids(db, tenant_id, [kb_id]))
        if not doc_ids:
            return {}
        # SqlMetadataStore supports direct doc_ids lookup
        if hasattr(store, "list_by_doc_ids") and not isinstance(store, type) and callable(getattr(store, "list_by_doc_ids", None)):
            try:
                return store.list_by_doc_ids(db, doc_ids)
            except NotImplementedError:
                pass
        # Fallback: list by kb_ids and filter
        tenant_id = cls._kb_tenant(db, kb_id)
        if not tenant_id:
            return {}
        doc_ids_set = set(doc_ids)
        result: dict[str, dict] = {}
        for did, meta in store.list_by_kb_ids(db, tenant_id, [kb_id]):
            if did in doc_ids_set:
                result[did] = meta
        return result

    # ── aggregate reads ────────────────────────────────────────────────────────

    @classmethod
    def get_flatted_meta_by_kbs(cls, db: Session, kb_ids: list[str]) -> dict:
        """Expanded aggregator — expands list values.

        Returns: {field: {value_str: [doc_ids]}}
        """
        if not kb_ids:
            return {}
        tenant_id = cls._kb_tenant(db, kb_ids[0])
        if not tenant_id:
            return {}
        rows = cls._store().list_by_kb_ids(db, tenant_id, kb_ids)
        meta: dict = {}
        for doc_id, fields in rows:
            if not isinstance(fields, dict):
                continue
            for k, v in fields.items():
                values = v if isinstance(v, list) else [v]
                for vv in values:
                    if vv is None:
                        continue
                    meta.setdefault(k, {}).setdefault(str(vv), []).append(doc_id)
        return meta

    @classmethod
    def get_metadata_summary(cls, db: Session, kb_id: str, doc_ids: list[str] | None = None) -> dict:
        """Return metadata summary with value frequencies and type inference."""

        def _type(value):
            if value is None:
                return None
            if isinstance(value, list):
                return "list"
            if isinstance(value, bool):
                return "string"
            if isinstance(value, (int, float)):
                return "number"
            if re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", str(value)):
                return "time"
            return "string"

        tenant_id = cls._kb_tenant(db, kb_id)
        if not tenant_id:
            return {}

        store = cls._store()
        if doc_ids and hasattr(store, "list_by_doc_ids") and callable(getattr(store, "list_by_doc_ids", None)):
            rows = list(store.list_by_doc_ids(db, doc_ids).items())
        else:
            rows = cls._store().list_by_kb_ids(db, tenant_id, [kb_id])
            if doc_ids:
                doc_ids_set = set(doc_ids)
                rows = [(did, meta) for did, meta in rows if did in doc_ids_set]

        summary: dict = {}
        type_counter: dict = {}

        for doc_id, fields in rows:
            if not isinstance(fields, dict):
                continue
            for k, v in fields.items():
                vt = _type(v)
                if vt:
                    type_counter.setdefault(k, {})[vt] = type_counter.get(k, {}).get(vt, 0) + 1
                for vv in v if isinstance(v, list) else [v]:
                    if vv is None:
                        continue
                    summary.setdefault(k, {})[str(vv)] = summary.get(k, {}).get(str(vv), 0) + 1

        result = {}
        for k, v in summary.items():
            values = sorted(v.items(), key=lambda x: x[1], reverse=True)
            type_counts = type_counter.get(k, {})
            vtype = max(type_counts.items(), key=lambda x: x[1])[0] if type_counts else "string"
            result[k] = {"type": vtype, "values": values}
        return result

    @classmethod
    def batch_update_metadata(cls, db: Session, kb_id: str, doc_ids: list[str], updates: list[dict] | None = None, deletes: list[dict] | None = None) -> int:
        """Batch update/delete metadata fields across multiple documents."""
        updates = updates or []
        deletes = deletes or []
        if not doc_ids:
            return 0

        tenant_id = cls._kb_tenant(db, kb_id)
        if not tenant_id:
            return 0

        store = cls._store()
        if hasattr(store, "list_by_doc_ids") and callable(getattr(store, "list_by_doc_ids", None)):
            rows = list(store.list_by_doc_ids(db, doc_ids).items())
        else:
            rows = store.list_by_kb_ids(db, tenant_id, [kb_id])
            doc_ids_set = set(doc_ids)
            rows = [(did, meta) for did, meta in rows if did in doc_ids_set]

        def _norm(meta):
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    return {}
            return deepcopy(meta) if isinstance(meta, dict) else {}

        def _apply_updates(meta: dict) -> bool:
            changed = False
            for upd in updates:
                key = upd.get("key")
                if not key:
                    continue
                new_value = upd.get("value")
                match_value = upd.get("match")
                match_provided = match_value is not None and match_value != ""
                if key not in meta:
                    if match_provided:
                        continue
                    meta[key] = dedupe_list(new_value) if isinstance(new_value, list) else new_value
                    changed = True
                    continue
                if isinstance(meta[key], list):
                    if not match_provided:
                        meta[key] = dedupe_list(meta[key] + (new_value if isinstance(new_value, list) else [new_value]))
                        changed = True
                    else:
                        new_list, replaced = [], False
                        for item in meta[key]:
                            if str(item) == str(match_value):
                                new_list.append(new_value)
                                replaced = True
                            else:
                                new_list.append(item)
                        if replaced:
                            meta[key] = dedupe_list(new_list)
                            changed = True
                else:
                    if not match_provided:
                        meta[key] = new_value
                        changed = True
                    elif str(meta[key]) == str(match_value):
                        meta[key] = new_value
                        changed = True
            return changed

        def _apply_deletes(meta: dict) -> bool:
            changed = False
            for d in deletes:
                key = d.get("key")
                if not key or key not in meta:
                    continue
                value = d.get("value")
                if isinstance(meta[key], list):
                    if value is None:
                        del meta[key]
                        changed = True
                    else:
                        new_list = [x for x in meta[key] if str(x) != str(value)]
                        if len(new_list) != len(meta[key]):
                            if new_list:
                                meta[key] = new_list
                            else:
                                del meta[key]
                            changed = True
                else:
                    if value is None or str(meta[key]) == str(value):
                        del meta[key]
                        changed = True
            return changed

        found_ids: set[str] = set()
        updated = 0

        for doc_id, raw_meta in rows:
            found_ids.add(doc_id)
            meta = _norm(raw_meta)
            orig = deepcopy(meta)
            changed = _apply_updates(meta)
            changed = _apply_deletes(meta) or changed
            if changed and meta != orig:
                if not meta:
                    store.delete(db, doc_id, tenant_id, kb_id)
                else:
                    processed = cls._split_combined_values(meta)
                    store.upsert(db, doc_id, tenant_id, kb_id, processed)
                updated += 1

        # Insert for docs without existing metadata rows
        doc_ids_set = set(doc_ids)
        missing = doc_ids_set - found_ids
        if missing and updates:
            for doc_id in missing:
                meta: dict = {}
                _apply_updates(meta)
                if meta:
                    processed = cls._split_combined_values(meta)
                    store.upsert(db, doc_id, tenant_id, kb_id, processed)
                    updated += 1

        return updated
