"""Plain callback boundaries for coordinated memory writes."""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypedDict, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from memory.revisions.domain import ModelBinding


class EmbeddingResult(TypedDict):
    vectors: list[list[float]]
    model_binding: ModelBinding


class ExtractionResult(TypedDict):
    items: list[dict[str, Any]]
    model_reference: str
    config_reference: str


@runtime_checkable
class WriteStorage(Protocol):
    async def read(self, message_id: int) -> dict[str, Any] | None: ...

    async def write(self, message: dict[str, Any]) -> None: ...

    async def set_status(self, message_id: int, status: bool) -> None: ...

    async def inventory(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class MessageIdAllocator(Protocol):
    async def __call__(self, *, minimum_message_id: int) -> int: ...


Authorize = Callable[[AsyncSession, str], Awaitable[None]]
BindingReader = Callable[[AsyncSession, str, int], Awaitable[ModelBinding]]
Embedder = Callable[[str, list[dict[str, Any]], ModelBinding | None], Awaitable[EmbeddingResult]]
Extractor = Callable[[str, dict[str, Any]], Awaitable[ExtractionResult]]
EnsureIndex = Callable[[str, int], Awaitable[None]]
FifoEvictor = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]
QueueSubmitter = Callable[[dict[str, Any]], Awaitable[bool]]


class WriterDrainSnapshot(TypedDict):
    state: str
    generation: int
    active_operations: list[dict[str, Any]]
    unknown_operations: list[dict[str, Any]]
    pending_operation_ids: list[str]
    untracked_task_ids: list[str]
    untracked_task_ids_sha256: str
    legacy_quiescence_attested: bool
    legacy_quiescence_attestation: dict[str, Any] | None
    drained: bool
