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
SqlMetadataStore — metadata persistence via independent SQL table (t_ai_document_metadata).

Used when the doc engine is Milvus.
"""

import logging

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from api.db.db_models import DocumentMetadata
from api.db.services.metadata_store import MetadataStore
from common.time_utils import current_timestamp, get_format_time

logger = logging.getLogger(__name__)


class SqlMetadataStore(MetadataStore):

    def get(self, db: Session, doc_id: str, tenant_id: str, kb_id: str) -> dict:
        row = db.get(DocumentMetadata, doc_id)
        if not row:
            return {}
        meta = row.meta_fields
        return meta if isinstance(meta, dict) else {}

    def upsert(self, db: Session, doc_id: str, tenant_id: str, kb_id: str,
               meta_fields: dict) -> bool:
        try:
            now_ts = current_timestamp()
            now_dt = get_format_time()
            stmt = pg_insert(DocumentMetadata).values(
                id=doc_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                meta_fields=meta_fields,
                create_time=now_ts,
                create_date=now_dt,
                update_time=now_ts,
                update_date=now_dt,
            ).on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "meta_fields": meta_fields,
                    "update_time": now_ts,
                    "update_date": now_dt,
                },
            )
            db.execute(stmt)
            db.commit()
            return True
        except Exception as e:
            db.rollback()
            logger.error("SqlMetadataStore.upsert failed for doc %s: %s", doc_id, e)
            return False

    def delete(self, db: Session, doc_id: str, tenant_id: str, kb_id: str) -> bool:
        try:
            db.execute(
                sa_delete(DocumentMetadata).where(DocumentMetadata.id == doc_id)
            )
            db.commit()
            return True
        except Exception as e:
            db.rollback()
            logger.error("SqlMetadataStore.delete failed for doc %s: %s", doc_id, e)
            return False

    def list_by_kb_ids(self, db: Session, tenant_id: str,
                       kb_ids: list[str]) -> list[tuple[str, dict]]:
        stmt = (
            select(DocumentMetadata.id, DocumentMetadata.meta_fields)
            .where(DocumentMetadata.kb_id.in_(kb_ids))
        )
        result: list[tuple[str, dict]] = []
        for row in db.execute(stmt).mappings():
            meta = row["meta_fields"] or {}
            if isinstance(meta, dict):
                result.append((row["id"], meta))
        return result

    def list_by_doc_ids(self, db: Session, doc_ids: list[str]) -> dict[str, dict]:
        if not doc_ids:
            return {}
        stmt = (
            select(DocumentMetadata.id, DocumentMetadata.meta_fields)
            .where(DocumentMetadata.id.in_(doc_ids))
        )
        result: dict[str, dict] = {}
        for row in db.execute(stmt).mappings():
            meta = row["meta_fields"] or {}
            if isinstance(meta, dict):
                result[row["id"]] = meta
        return result
