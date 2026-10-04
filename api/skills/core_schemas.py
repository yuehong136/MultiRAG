"""RAGFlow core request DTOs, independent from immutable asset publication."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.skills.schemas import SkillError


def field_defaults() -> dict[str, dict[str, Any]]:
    return {"name": {"enabled": True, "weight": 3.0}, "tags": {"enabled": True, "weight": 2.0}, "description": {"enabled": True, "weight": 1.0}, "content": {"enabled": False, "weight": 0.5}}


class CoreRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    @model_validator(mode="before")
    @classmethod
    def require_space(cls, value: Any) -> Any:
        if "space_id" in cls.model_fields and isinstance(value, dict):
            identity = value.get("space_id")
            if identity is None or (isinstance(identity, str) and not identity.strip()):
                raise SkillError(400, "SPACE_REQUIRED", "An explicit space_id is required")
        return value


class CoreCreate(CoreRequest):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    embd_id: str = ""
    rerank_id: str = ""


class CoreUpdate(CoreRequest):
    name: str | None = Field(default=None, max_length=128)
    description: str | None = None
    embd_id: str | None = None
    rerank_id: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=10000)


class CoreConfigRequest(CoreRequest):
    space_id: str = ""
    embd_id: str
    vector_similarity_weight: float = Field(default=0.3, ge=0, le=1)
    similarity_threshold: float = Field(default=0.2, ge=0, le=1)
    field_config: dict[str, Any] = Field(default_factory=field_defaults)
    rerank_id: str = ""
    top_k: int = Field(default=10, ge=1, le=10000)


class CoreSearchRequest(CoreRequest):
    space_id: str = ""
    query: str = ""
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=1000)
    sort_by: str = ""
    sort_order: str = ""


class CoreSkillInfo(CoreRequest):
    id: str = ""
    folder_id: str = ""
    name: str = ""
    description: str = ""
    tags: list[str] | None = None
    content: str = ""
    version: str = ""


class CoreIndexRequest(CoreRequest):
    space_id: str = ""
    embd_id: str = ""
    skills: list[CoreSkillInfo] = Field(max_length=1000)


class CoreReindexRequest(CoreRequest):
    space_id: str
    embd_id: str = ""
