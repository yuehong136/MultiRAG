"""Validation helpers for document upload manifests."""

from __future__ import annotations

import unicodedata
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.constants import FILE_NAME_LEN_LIMIT


class UploadManifestValidationError(ValueError):
    """Raised when a manifest cannot be mapped safely to uploaded files."""


class UploadDocumentManifestItem(BaseModel):
    """Caller-supplied document name for one uploaded file."""

    model_config = ConfigDict(extra="forbid")

    file_index: int = Field(ge=0, description="Zero-based index of the matching files part.")
    name: str = Field(description="Document filename used by parsing and retrieval, including the original extension.")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFC", value).strip()
        if not normalized or normalized in {".", ".."}:
            raise ValueError("Document name must not be empty.")
        if "/" in normalized or "\\" in normalized:
            raise ValueError("Document name must not contain path separators.")
        if any(unicodedata.category(char) == "Cc" for char in normalized):
            raise ValueError("Document name must not contain control characters.")
        if len(normalized.encode("utf-8")) > FILE_NAME_LEN_LIMIT:
            raise ValueError(f"Document name must be {FILE_NAME_LEN_LIMIT} UTF-8 bytes or less.")
        return normalized


class UploadDocumentsManifest(BaseModel):
    """Strict per-file metadata for one batch upload request."""

    model_config = ConfigDict(extra="forbid")

    documents: list[UploadDocumentManifestItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_file_indexes(self) -> UploadDocumentsManifest:
        indexes = [document.file_index for document in self.documents]
        if len(indexes) != len(set(indexes)):
            raise ValueError("manifest.documents contains duplicate file_index values.")
        return self


def resolve_document_upload_names(source_filenames: list[str], manifest: UploadDocumentsManifest | None) -> list[str]:
    """Resolve document names while preserving the legacy filename fallback."""
    if manifest is None:
        return source_filenames

    expected_indexes = set(range(len(source_filenames)))
    documents_by_index = {document.file_index: document for document in manifest.documents}
    actual_indexes = set(documents_by_index)
    if actual_indexes != expected_indexes:
        raise UploadManifestValidationError(f"manifest.documents must contain each files index exactly once (expected {sorted(expected_indexes)}, got {sorted(actual_indexes)}).")

    resolved_names: list[str] = []
    for file_index, source_filename in enumerate(source_filenames):
        source_basename = PurePosixPath(source_filename.replace("\\", "/")).name
        source_suffix = PurePosixPath(source_basename).suffix
        requested_name = documents_by_index[file_index].name
        requested_suffix = PurePosixPath(requested_name).suffix
        if not source_suffix or requested_suffix.lower() != source_suffix.lower():
            raise UploadManifestValidationError(f"manifest.documents[{file_index}].name must preserve the uploaded file extension {source_suffix or '<missing>'!r}.")
        resolved_names.append(requested_name)

    return resolved_names
