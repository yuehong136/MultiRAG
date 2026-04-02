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
MetadataStore — abstract interface for document metadata persistence.

Two implementations:
- SqlMetadataStore: for Milvus backend (metadata in independent SQL table)
- EngineMetadataStore: for ES/Infinity backend (metadata in docStoreConn sidecar index)
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from sqlalchemy.orm import Session


class MetadataStore(ABC):
    """Uniform storage interface consumed by DocMetadataService."""

    @abstractmethod
    def get(self, db: Session, doc_id: str, tenant_id: str, kb_id: str) -> dict:
        """Return meta_fields dict for a single document, or {} if not found."""

    @abstractmethod
    def upsert(self, db: Session, doc_id: str, tenant_id: str, kb_id: str,
               meta_fields: dict) -> bool:
        """Insert or update metadata for a document. Return True on success."""

    @abstractmethod
    def delete(self, db: Session, doc_id: str, tenant_id: str, kb_id: str) -> bool:
        """Delete metadata for a document. Return True on success."""

    @abstractmethod
    def list_by_kb_ids(self, db: Session, tenant_id: str,
                       kb_ids: list[str]) -> list[tuple[str, dict]]:
        """Return [(doc_id, meta_fields), ...] for all docs in the given kb_ids."""

    @abstractmethod
    def list_by_doc_ids(self, db: Session, doc_ids: list[str]) -> dict[str, dict]:
        """Return {doc_id: meta_fields} for the given doc_ids."""

    def delete_tenant_container(self, tenant_id: str) -> bool:
        """Delete tenant-scoped metadata container if the backend has one."""
        return True
