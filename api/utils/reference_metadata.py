"""Reference metadata preferences and dataset-scoped enrichment.

Only the current document metadata dictionary contract is consumed. Missing
fields selects all keys; an explicit empty list selects none.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, model_serializer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.services.doc_metadata_service import DocMetadataService


class ReferenceMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include: StrictBool = False
    fields: list[StrictStr] | None = None

    @model_serializer
    def serialize(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.model_fields_set}


def resolve_reference_metadata_preferences(
    request_payload: Mapping[str, Any] | None = None,
    config_payload: Mapping[str, Any] | None = None,
) -> tuple[bool, set[str] | None]:
    resolved: dict[str, Any] = {}
    for payload in (config_payload, request_payload):
        value = (payload or {}).get("reference_metadata")
        if value is not None:
            config = ReferenceMetadata.model_validate(value)
            resolved.update(config.model_dump(exclude_unset=True))
    preferences = ReferenceMetadata.model_validate(resolved)
    return preferences.include, None if preferences.fields is None else set(preferences.fields)


def reference_dataset_id(value: Any) -> str | None:
    """A graph chunk spanning multiple datasets has no unambiguous document pair."""
    if isinstance(value, (list, tuple)) and len(value) == 1:
        value = value[0]
    return value if isinstance(value, str) and value else None


def enrich_reference_metadata(
    db: Session,
    chunks: list[dict[str, Any]],
    preferences: tuple[bool, set[str] | None],
    *,
    kb_field: str = "kb_id",
    doc_field: str = "doc_id",
) -> None:
    pairs = _reference_pairs(chunks, preferences, kb_field, doc_field)
    metadata = {kb_id: DocMetadataService.get_metadata_for_documents(db, sorted(doc_ids), kb_id) for kb_id, doc_ids in pairs.items()}
    _apply_metadata(chunks, metadata, preferences[1], kb_field, doc_field)


def _reference_pairs(chunks: list[dict[str, Any]], preferences: tuple[bool, set[str] | None], kb_field: str, doc_field: str) -> dict[str, set[str]]:
    include, fields = preferences
    pairs: dict[str, set[str]] = {}
    for chunk in chunks:
        chunk.pop("document_metadata", None)
        kb_id = reference_dataset_id(chunk.get(kb_field))
        doc_id = chunk.get(doc_field)
        if include and fields != set() and kb_id and isinstance(doc_id, str) and doc_id:
            pairs.setdefault(kb_id, set()).add(doc_id)
    return pairs


def _apply_metadata(chunks: list[dict[str, Any]], metadata: dict[str, dict[str, dict]], fields: set[str] | None, kb_field: str, doc_field: str) -> None:
    for chunk in chunks:
        kb_id = reference_dataset_id(chunk.get(kb_field))
        doc_id = chunk.get(doc_field)
        meta = metadata.get(kb_id or "", {}).get(doc_id, {}) if isinstance(doc_id, str) else {}
        if not isinstance(meta, dict):
            continue
        selected = {key: value for key, value in meta.items() if fields is None or key in fields}
        if selected:
            chunk["document_metadata"] = selected


async def enrich_reference_metadata_async(
    db: AsyncSession,
    chunks: list[dict[str, Any]],
    preferences: tuple[bool, set[str] | None],
    *,
    kb_field: str = "kb_id",
    doc_field: str = "doc_id",
) -> None:
    pairs = _reference_pairs(chunks, preferences, kb_field, doc_field)
    metadata = {kb_id: await DocMetadataService.get_metadata_for_documents_async(db, sorted(doc_ids), kb_id) for kb_id, doc_ids in pairs.items()}
    _apply_metadata(chunks, metadata, preferences[1], kb_field, doc_field)
