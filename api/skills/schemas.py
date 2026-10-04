"""Versioned Skill Asset API types. Model identifiers stay strings on the wire."""

import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SkillError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSpace(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = unicodedata.normalize("NFC", value.strip())
        if not value or len(value) > 128 or any(unicodedata.category(c).startswith("C") for c in value):
            raise ValueError("Invalid space name")
        return value


class UpdateSpace(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    revision: int = Field(gt=0)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return CreateSpace.clean_name(value) if value is not None else None


class ManifestFile(StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0, le=5 * 1024 * 1024)


class UploadManifest(StrictModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: str = Field(min_length=1, max_length=128)
    activate: bool = False
    files: list[ManifestFile] | None = Field(default=None, max_length=1000)

    @field_validator("version")
    @classmethod
    def semver(cls, value: str) -> str:
        number = r"(?:0|[1-9][0-9]*)"
        prerelease = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
        pattern = rf"{number}\.{number}\.{number}(?:-{prerelease}(?:\.{prerelease})*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
        if re.fullmatch(pattern, value) is None:
            raise ValueError("Invalid SemVer")
        return value


class ActivateVersion(StrictModel):
    version_id: str | None
    revision: int = Field(gt=0)


class DeleteMany(StrictModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class FieldWeight(StrictModel):
    enabled: bool
    weight: float = Field(ge=0, le=10, allow_inf_nan=False)


def default_fields() -> dict[str, dict[str, Any]]:
    return {name: {"enabled": name != "content", "weight": weight} for name, weight in (("name", 3.0), ("tags", 2.0), ("description", 1.0), ("content", 0.5))}


class UpdateConfig(StrictModel):
    revision: int = Field(gt=0)
    embedding_model_id: str | None = None
    rerank_model_id: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=100)
    vector_weight: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    similarity_threshold: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    fields: dict[Literal["name", "tags", "description", "content"], FieldWeight] | None = None

    @field_validator("embedding_model_id", "rerank_model_id")
    @classmethod
    def model_id(cls, value: str | None) -> str | None:
        if value is not None and (re.fullmatch(r"[1-9][0-9]*", value) is None or int(value) > 2**63 - 1):
            raise ValueError("Model ID must be a positive BIGINT decimal string")
        return value

    @model_validator(mode="after")
    def validate_fields(self) -> "UpdateConfig":
        for name in self.model_fields_set - {"embedding_model_id", "rerank_model_id"}:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        if self.fields is not None and (set(self.fields) != {"name", "tags", "description", "content"} or not any(v.enabled and v.weight > 0 for v in self.fields.values())):
            raise ValueError("All four fields and at least one positive enabled weight are required")
        return self


class SearchRequest(StrictModel):
    query: str = Field(default="", max_length=8192)
    mode: Literal["keyword", "vector", "hybrid"] = "hybrid"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def empty_query(self) -> "SearchRequest":
        if not self.query.strip() and self.mode != "keyword":
            raise ValueError("Empty query requires keyword mode")
        return self


def idempotency_key(value: str | None) -> str:
    if value is None or not 1 <= len(value) <= 128 or any(not 33 <= ord(c) <= 126 for c in value):
        raise SkillError(400, "IDEMPOTENCY_KEY_REQUIRED", "A visible ASCII Idempotency-Key of 1–128 characters is required")
    return value
