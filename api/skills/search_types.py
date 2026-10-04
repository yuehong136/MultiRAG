"""Backend-independent Skills search values and failure boundary."""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

FIELDS = ("name", "tags", "description", "content")


class SkillSearchError(RuntimeError):
    def __init__(self, error_code: str, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class SkillIndexDocument:
    skill_id: str
    version_id: str
    name: str
    description: str
    tags: list[str]
    version: str
    content: str
    content_digest: str


@dataclass(frozen=True)
class SkillSearchHit:
    version_id: str
    skill_id: str
    score: float
    text: str = ""


@dataclass(frozen=True)
class IndexMatch:
    version_id: str
    skill_id: str
    score: float
    text: str


@runtime_checkable
class IndexStore(Protocol):
    def create(self, name: str, dimension: int) -> None: ...

    def insert(self, name: str, rows: list[dict[str, Any]]) -> None: ...

    def seal(self, name: str, expected: list[dict[str, Any]]) -> None: ...

    def search(self, name: str, field: str, query: str | list[float], mode: str, limit: int) -> list[IndexMatch]: ...

    def delete(self, name: str, version_ids: list[str] | None = None) -> None: ...

    def count(self, name: str) -> int: ...


@runtime_checkable
class SearchModels(Protocol):
    async def models(self, tenant_id: str) -> list[dict[str, Any]]: ...

    async def resolve(self, tenant_id: str, model_id: Any, model_type: str) -> dict[str, Any]: ...

    async def encode(self, tenant_id: str, config: dict[str, Any], texts: list[str], *, query: bool = False) -> Any: ...

    async def rerank(self, tenant_id: str, config: dict[str, Any], query: str, texts: list[str]) -> Any: ...
