"""Shared GraphRAG execution budgets for API validation and worker replay."""

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

Attempts = Annotated[int, Field(strict=True, ge=1, le=10)]
PositiveSeconds = Annotated[int, Field(strict=True, ge=1, le=86400)]
OptionalTimeout = Annotated[int, Field(strict=True, ge=0, le=86400)]


class GraphRAGExecutionConfig(BaseModel):
    """Missing fields use defaults; explicitly invalid persisted values fail.

    Phase timeout 0 disables that timeout; lock timeout 0 uses the bounded
    600-second default. Zero backoff means immediate retry.
    """

    model_config = ConfigDict(strict=True, extra="ignore", allow_inf_nan=False)
    batch_chunk_token_size: Annotated[int, Field(strict=True, ge=512, le=8196)] = 4096
    retry_attempts: Attempts = 2
    retry_backoff_seconds: Annotated[float, Field(ge=0, le=600)] = 2.0
    retry_backoff_max_seconds: Annotated[float, Field(ge=0, le=3600)] = 60.0
    build_subgraph_timeout_per_chunk_seconds: PositiveSeconds = 300
    build_subgraph_min_timeout_seconds: PositiveSeconds = 600
    merge_timeout_seconds: OptionalTimeout = 180
    resolution_timeout_seconds: OptionalTimeout = 1800
    community_timeout_seconds: OptionalTimeout = 1800
    lock_acquire_timeout_seconds: OptionalTimeout = 600
    build_subgraph_retry_attempts: Attempts | None = Field(default=None, exclude_if=lambda value: value is None)
    merge_retry_attempts: Attempts | None = Field(default=None, exclude_if=lambda value: value is None)
    resolution_retry_attempts: Attempts | None = Field(default=None, exclude_if=lambda value: value is None)
    community_retry_attempts: Attempts | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="before")
    @classmethod
    def explicit_values(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for name in cls.model_fields:
                if name in value and value[name] is None:
                    raise ValueError(f"GraphRAG {name} cannot be null")
        return value

    @property
    def lock_wait_seconds(self) -> int:
        return self.lock_acquire_timeout_seconds or 600
