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
EngineMetadataStore — metadata persistence via docStoreConn sidecar index.

Used when the doc engine is Elasticsearch, OpenSearch, Infinity or OceanBase.
Index name pattern: multirag_doc_meta_{tenant_id}
"""

import json
import logging
from collections.abc import Iterator
from contextlib import closing
from typing import Any

from sqlalchemy.orm import Session

from api.db.services.metadata_store import MetadataStore
from common import settings

logger = logging.getLogger(__name__)


class EngineMetadataStore(MetadataStore):
    # ── helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_metadata(flat: dict) -> dict:
        if not flat or not isinstance(flat, dict):
            return {}
        meta = flat.get("meta_fields")
        if not meta:
            return {}
        if isinstance(meta, str):
            try:
                return json.loads(meta)
            except json.JSONDecodeError:
                return {}
        return meta if isinstance(meta, dict) else {}

    @staticmethod
    def _extract_doc_id(doc: dict, hit: dict | None = None) -> str:
        if hit:
            return hit.get("_id", "")
        return doc.get("doc_id") or doc.get("_id") or doc.get("id", "")

    @classmethod
    def _iter_results(cls, results):
        """Yield (doc_id, doc_dict) from any docStoreConn result format."""
        if isinstance(results, tuple) and len(results) == 2:
            results = results[0]

        if hasattr(results, "iterrows"):
            for _, row in results.iterrows():
                doc = dict(row)
                doc_id = cls._extract_doc_id(doc)
                if doc_id:
                    yield doc_id, doc
        elif hasattr(results, "get") and "hits" in results:
            for hit in results.get("hits", {}).get("hits", []):
                doc = hit.get("_source", {})
                doc_id = cls._extract_doc_id(doc, hit)
                if doc_id:
                    yield doc_id, doc
        # Check if OceanBase SearchResult format
        elif hasattr(results, "chunks") and hasattr(results, "total"):
            for doc in results.chunks:
                doc_id = cls._extract_doc_id(doc)
                if doc_id:
                    yield doc_id, doc
        elif isinstance(results, list):
            for res in results:
                docs = [res] if isinstance(res, dict) else res
                for doc in docs:
                    doc_id = cls._extract_doc_id(doc)
                    if doc_id:
                        yield doc_id, doc

    @staticmethod
    def _ensure_index(index_name: str) -> bool:
        if settings.docStoreConn.index_exist(index_name, ""):
            return True
        logger.debug("Creating metadata index %s", index_name)
        result = settings.docStoreConn.create_doc_meta_idx(index_name)
        if result is False:
            logger.error("Failed to create metadata index %s", index_name)
            return False
        return True

    @staticmethod
    def _drop_if_empty(index_name: str) -> None:
        try:
            if not settings.docStoreConn.index_exist(index_name, ""):
                return
            count_idx = getattr(settings.docStoreConn, "count_idx", None)
            count = count_idx(index_name) if callable(count_idx) else -1
            if type(count) is int and count >= 0:
                if count == 0:
                    settings.docStoreConn.delete_idx(index_name, "")
                return
            if callable(count_idx):
                logger.warning("Unknown metadata count; retaining index %s", index_name)
                return
            # Backends without a native count retain the search fallback.
            from common.doc_store.doc_store_base import OrderByExpr

            results = settings.docStoreConn.search(
                select_fields=["id"],
                highlight_fields=[],
                condition={},
                match_expressions=[],
                order_by=OrderByExpr(),
                offset=0,
                limit=1,
                index_names=index_name,
                knowledgebase_ids=[""],
            )
            total = None
            if isinstance(results, tuple):
                _, total = results
            elif hasattr(results, "total"):
                total = results.total
            elif hasattr(results, "get") and "hits" in results:
                raw = results["hits"].get("total")
                if isinstance(raw, dict):
                    if raw.get("relation") == "eq":
                        total = raw.get("value")
                else:
                    total = raw
                if results.get("timed_out") is not False or results.get("_shards", {}).get("failed") != 0:
                    return
            if type(total) is int and total == 0:
                settings.docStoreConn.delete_idx(index_name, "")
        except Exception as e:
            logger.warning("Failed to check/drop empty metadata index %s: %s", index_name, e)

    @classmethod
    def _iter_es_rows(cls, index_name: str, kb_ids: list[str], query: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
        """Consume a scroll snapshot completely; partial RPCs are failures."""
        with closing(settings.docStoreConn.iter_doc_metadata(index_name, kb_ids, query)) as rows:
            yield from rows

    @classmethod
    def iter_metadata(cls, index: str, kb_ids: list[str], query: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
        """Close the underlying scroll even if the exact matcher raises."""
        with closing(cls._iter_es_rows(index, kb_ids, query)) as rows:
            for doc_id, source in rows:
                yield doc_id, cls._extract_metadata(source)

    def filter_candidates(self, tenant_id: str, kb_ids: list[str], filters: list[dict[str, Any]], logic: str) -> Iterator[tuple[str, dict[str, Any]]] | None:
        from common.metadata_es_filter import UnsupportedMetaFilter, build_meta_filter_query

        index = self._index_name(tenant_id)
        if not settings.docStoreConn.doc_meta_index_exists(index):
            return iter(())
        mapping = settings.docStoreConn.doc_meta_mapping(index)
        try:
            query = build_meta_filter_query(filters, logic, kb_ids, mapping)
        except UnsupportedMetaFilter:
            return None
        return self.iter_metadata(index, kb_ids, query)

    @classmethod
    def _search_all(cls, index_name: str, condition: dict | None, kb_ids: list[str]) -> list[dict]:
        if settings.DOC_ENGINE.lower() in {"elasticsearch", "opensearch"} and condition == {"kb_id": kb_ids}:
            # Never translate an RPC error into a missing metadata index.
            if not settings.docStoreConn.doc_meta_index_exists(index_name):
                return []
            query = {"bool": {"filter": [{"terms": {"kb_id": kb_ids}}]}}
            return [{**source, "id": doc_id} for doc_id, source in cls._iter_es_rows(index_name, kb_ids, query)]
        if not settings.docStoreConn.index_exist(index_name, ""):
            logger.debug("Metadata index %s does not exist, skipping search", index_name)
            return []

        from common.doc_store.doc_store_base import OrderByExpr

        # Add sort by id for ES to enable search_after on large data
        order_by = OrderByExpr()
        if not settings.DOC_ENGINE_INFINITY and not settings.DOC_ENGINE_OCEANBASE:
            order_by.asc("id")
        page_size = 1000
        all_docs: list[dict] = []
        page = 0

        while True:
            results = settings.docStoreConn.search(
                select_fields=["*"],
                highlight_fields=[],
                condition=condition or {},
                match_expressions=[],
                order_by=order_by,
                offset=page * page_size,
                limit=page_size,
                index_names=index_name,
                knowledgebase_ids=kb_ids,
            )
            if results is None:
                break

            page_docs: list[dict] = []
            total_count = None

            if isinstance(results, tuple) and len(results) == 2:
                df, total_count = results
                if hasattr(df, "iterrows"):
                    page_docs = df.to_dict("records")
                else:
                    page_docs = list(df) if df else []
            elif hasattr(results, "chunks") and hasattr(results, "total"):
                # OceanBase SearchResult format
                page_docs = results.chunks
                total_count = results.total
            elif hasattr(results, "get") and "hits" in results:
                hits_obj = results.get("hits", {})
                hits = hits_obj.get("hits", [])
                for hit in hits:
                    doc = hit.get("_source", {})
                    doc["id"] = hit.get("_id", "")
                    page_docs.append(doc)
                total_raw = hits_obj.get("total", {})
                total_count = total_raw.get("value", len(page_docs)) if isinstance(total_raw, dict) else total_raw
            elif hasattr(results, "__iter__") and not isinstance(results, dict):
                page_docs = list(results)

            if not page_docs:
                break

            all_docs.extend(page_docs)
            page += 1

            if total_count is not None:
                if len(all_docs) >= total_count:
                    break
            else:
                if len(page_docs) < page_size:
                    break

        return all_docs

    # ── MetadataStore interface ────────────────────────────────────────────────

    def _index_name(self, tenant_id: str) -> str:
        return f"multirag_doc_meta_{tenant_id}"

    def get(self, db: Session, doc_id: str, tenant_id: str, kb_id: str) -> dict:
        index_name = self._index_name(tenant_id)
        try:
            result = settings.docStoreConn.get(doc_id, index_name, [kb_id])
            if result:
                return self._extract_metadata(result)
        except Exception as e:
            logger.error("Error getting metadata for doc %s: %s", doc_id, e)
        return {}

    def upsert(self, db: Session, doc_id: str, tenant_id: str, kb_id: str, meta_fields: dict) -> bool:
        index_name = self._index_name(tenant_id)

        if not settings.DOC_ENGINE_INFINITY and not settings.DOC_ENGINE_OCEANBASE:
            # Native whole-map replacement must not degrade into destructive delete+insert.
            try:
                if not self._ensure_index(index_name):
                    return False
                existing = settings.docStoreConn.get(doc_id, index_name, [kb_id])
                if existing:
                    if existing.get("kb_id") != kb_id:
                        logger.error("Metadata replacement scope mismatch: %s", doc_id)
                        return False
                    return settings.docStoreConn.replace_meta_fields(index_name, doc_id, meta_fields or {})
                return self._insert(index_name, doc_id, kb_id, meta_fields)
            except Exception:
                logger.exception("Metadata upsert failed: %s", doc_id)
                return False

        # Infinity/OceanBase: delete + insert
        self._delete_from_store(index_name, doc_id, kb_id)
        return self._insert(index_name, doc_id, kb_id, meta_fields)

    def _insert(self, index_name: str, doc_id: str, kb_id: str, meta_fields: dict) -> bool:
        if not self._ensure_index(index_name):
            return False
        doc_meta = {"id": doc_id, "kb_id": kb_id, "meta_fields": meta_fields or {}}
        err = settings.docStoreConn.insert([doc_meta], index_name, kb_id)
        if err:
            logger.error("Failed to insert metadata for doc %s: %s", doc_id, err)
            return False
        # ES/OS insert already waits for visibility; an extra refresh costs a
        # second RPC without strengthening the successful-write contract.
        return True

    def _delete_from_store(self, index_name: str, doc_id: str, kb_id: str):
        if settings.docStoreConn.index_exist(index_name, ""):
            settings.docStoreConn.delete({"id": doc_id}, index_name, kb_id)

    def delete(self, db: Session, doc_id: str, tenant_id: str, kb_id: str) -> bool:
        index_name = self._index_name(tenant_id)
        if not settings.docStoreConn.index_exist(index_name, ""):
            return True
        settings.docStoreConn.delete({"id": doc_id}, index_name, kb_id)
        return True

    def list_by_kb_ids(self, db: Session, tenant_id: str, kb_ids: list[str]) -> list[tuple[str, dict]]:
        index_name = self._index_name(tenant_id)
        out: list[tuple[str, dict]] = []
        results = self._search_all(index_name, {"kb_id": kb_ids}, kb_ids)
        for doc_id, doc in self._iter_results(results):
            meta = self._extract_metadata(doc)
            out.append((doc_id, meta))
        return out

    def list_by_doc_ids(self, db: Session, doc_ids: list[str]) -> dict[str, dict]:
        if not doc_ids:
            return {}

        from sqlalchemy import select

        from api.db.db_models import Document, Knowledgebase
        from common.doc_store.doc_store_base import OrderByExpr

        rows = db.execute(select(Document.id, Document.kb_id, Knowledgebase.tenant_id).join(Knowledgebase, Knowledgebase.id == Document.kb_id).where(Document.id.in_(doc_ids))).all()

        grouped: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            grouped.setdefault((row.tenant_id, row.kb_id), []).append(row.id)

        metadata_map: dict[str, dict] = {}

        for (tenant_id, kb_id), group_doc_ids in grouped.items():
            index_name = self._index_name(tenant_id)
            if not settings.docStoreConn.index_exist(index_name, ""):
                continue

            if not settings.DOC_ENGINE_INFINITY and not settings.DOC_ENGINE_OCEANBASE:
                for doc in settings.docStoreConn.get_doc_metadata_rows(index_name, group_doc_ids, kb_id):
                    meta = self._extract_metadata(doc)
                    if meta:
                        metadata_map[doc["id"]] = meta
                continue

            results = settings.docStoreConn.search(
                select_fields=["*"],
                highlight_fields=[],
                condition={"id": group_doc_ids},
                match_expressions=[],
                order_by=OrderByExpr(),
                offset=0,
                limit=max(len(group_doc_ids), 1),
                index_names=index_name,
                knowledgebase_ids=[kb_id],
            )
            for doc_id, doc in self._iter_results(results):
                meta = self._extract_metadata(doc)
                if meta:
                    metadata_map[doc_id] = meta

        return metadata_map

    def delete_tenant_container(self, tenant_id: str) -> bool:
        index_name = self._index_name(tenant_id)
        try:
            if settings.docStoreConn.index_exist(index_name, ""):
                settings.docStoreConn.delete_idx(index_name, "")
            return True
        except Exception as e:
            logger.warning("Failed to delete metadata index %s: %s", index_name, e)
            return False
