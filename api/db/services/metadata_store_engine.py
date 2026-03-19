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

Used when the doc engine is Elasticsearch or Infinity.
Index name pattern: multirag_doc_meta_{tenant_id}
"""

import json
import logging

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
        elif hasattr(results, 'chunks') and hasattr(results, 'total'):
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
    def _drop_if_empty(index_name: str):
        try:
            if not settings.docStoreConn.index_exist(index_name, ""):
                return
            if not settings.DOC_ENGINE_INFINITY and not settings.DOC_ENGINE_OCEANBASE:
                count_resp = settings.docStoreConn.es.count(index=index_name)
                if count_resp.get("count", 1) == 0:
                    settings.docStoreConn.delete_idx(index_name, "")
            else:
                from common.doc_store.doc_store_base import OrderByExpr
                results = settings.docStoreConn.search(
                    select_fields=["id"], highlight_fields=[],
                    condition={}, match_expressions=[],
                    order_by=OrderByExpr(), offset=0, limit=1,
                    index_names=index_name, knowledgebase_ids=[""],
                )
                total = None
                if isinstance(results, tuple):
                    _, total = results
                elif hasattr(results, 'total'):
                    total = results.total
                if total is not None and total == 0:
                    settings.docStoreConn.delete_idx(index_name, "")
        except Exception as e:
            logger.warning("Failed to check/drop empty metadata index %s: %s", index_name, e)

    @classmethod
    def _search_all(cls, index_name: str, condition: dict | None,
                    kb_ids: list[str]) -> list[dict]:
        if not settings.docStoreConn.index_exist(index_name, ""):
            logger.debug("Metadata index %s does not exist, skipping search", index_name)
            return []

        from common.doc_store.doc_store_base import OrderByExpr

        order_by = OrderByExpr()
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
            elif hasattr(results, 'chunks') and hasattr(results, 'total'):
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

    def upsert(self, db: Session, doc_id: str, tenant_id: str, kb_id: str,
               meta_fields: dict) -> bool:
        index_name = self._index_name(tenant_id)

        if not settings.DOC_ENGINE_INFINITY and not settings.DOC_ENGINE_OCEANBASE:
            # ES: partial update or insert
            if not settings.docStoreConn.index_exist(index_name, ""):
                settings.docStoreConn.create_doc_meta_idx(index_name)
                return self._insert(index_name, doc_id, kb_id, meta_fields)
            try:
                doc_exists = settings.docStoreConn.get(doc_id, index_name, [kb_id])
                if doc_exists:
                    settings.docStoreConn.es.update(
                        index=index_name, id=doc_id, refresh=True,
                        doc={"meta_fields": meta_fields}
                    )
                    return True
            except Exception:
                pass
            return self._insert(index_name, doc_id, kb_id, meta_fields)

        # Infinity/OceanBase: delete + insert
        self._delete_from_store(index_name, doc_id, kb_id)
        return self._insert(index_name, doc_id, kb_id, meta_fields)

    def _insert(self, index_name: str, doc_id: str, kb_id: str,
                meta_fields: dict) -> bool:
        if not self._ensure_index(index_name):
            return False
        doc_meta = {"id": doc_id, "kb_id": kb_id, "meta_fields": meta_fields or {}}
        err = settings.docStoreConn.insert([doc_meta], index_name, kb_id)
        if err:
            logger.error("Failed to insert metadata for doc %s: %s", doc_id, err)
            return False
        if not settings.DOC_ENGINE_INFINITY and not settings.DOC_ENGINE_OCEANBASE:
            try:
                settings.docStoreConn.es.indices.refresh(index=index_name)
            except Exception:
                pass
        return True

    def _delete_from_store(self, index_name: str, doc_id: str, kb_id: str):
        if settings.docStoreConn.index_exist(index_name, ""):
            settings.docStoreConn.delete({"id": doc_id}, index_name, kb_id)

    def delete(self, db: Session, doc_id: str, tenant_id: str, kb_id: str,
               skip_empty_check: bool = False) -> bool:
        index_name = self._index_name(tenant_id)
        if not settings.docStoreConn.index_exist(index_name, ""):
            return True
        settings.docStoreConn.delete({"id": doc_id}, index_name, kb_id)
        if not skip_empty_check:
            self._drop_if_empty(index_name)
        return True

    def list_by_kb_ids(self, db: Session, tenant_id: str,
                       kb_ids: list[str]) -> list[tuple[str, dict]]:
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

        rows = db.execute(
            select(Document.id, Document.kb_id, Knowledgebase.tenant_id)
            .join(Knowledgebase, Knowledgebase.id == Document.kb_id)
            .where(Document.id.in_(doc_ids))
        ).all()

        grouped: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            grouped.setdefault((row.tenant_id, row.kb_id), []).append(row.id)

        metadata_map: dict[str, dict] = {}

        for (tenant_id, kb_id), group_doc_ids in grouped.items():
            index_name = self._index_name(tenant_id)
            if not settings.docStoreConn.index_exist(index_name, ""):
                continue

            if not settings.DOC_ENGINE_INFINITY and not settings.DOC_ENGINE_OCEANBASE:
                try:
                    response = settings.docStoreConn.es.mget(
                        index=index_name,
                        ids=group_doc_ids,
                        _source=True,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to batch fetch metadata from ES index %s: %s",
                        index_name,
                        e,
                    )
                    continue

                for hit in response.get("docs", []):
                    if not hit.get("found"):
                        continue
                    doc = hit.get("_source", {})
                    doc["id"] = hit.get("_id", "")
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
