"""Strict transport values for optional, explicitly chosen target conditions."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def decimal_version(value: Any) -> Any:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19 and value == str(int(value)):
        return int(value)
    return value


TargetVersion = Annotated[int, BeforeValidator(decimal_version), Field(strict=True, ge=1, le=2**63 - 1)]
TargetHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class TargetScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(max_length=256)
    agent_id: str = Field(max_length=256)
    session_id: str = Field(max_length=256)


class TargetSourceCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: TargetVersion
    source_revision: TargetVersion
    source_generation: TargetVersion
    content_sha256: TargetHash
    scope: TargetScope
    projection_sha256: TargetHash


class TargetEntryCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: TargetVersion
    message_id: TargetVersion
    generation: TargetVersion
    content_sha256: TargetHash
    scope: TargetScope
    projection_sha256: TargetHash
    source_versions: list[TargetSourceCondition] = Field(min_length=1, max_length=32)


class TargetModelBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_model_id: TargetVersion | None = None
    model_reference: str = Field(min_length=1, max_length=512)
    dimension: int = Field(strict=True, ge=1, le=32768)
    config_reference: str = Field(max_length=256)


class TargetPreconditions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: list[TargetSourceCondition] = Field(min_length=1, max_length=8)
    targets: list[TargetEntryCondition] = Field(min_length=1, max_length=8)
    model_binding: TargetModelBinding


class RecommendTargetsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_ids: list[TargetVersion] = Field(min_length=1, max_length=8)
    user_id: str = Field(max_length=256)
    agent_id: str = Field(max_length=256)
    query: str | None = Field(default=None, min_length=1, max_length=4096)
    retrieval_mode: Literal["hybrid", "lexical"] = "hybrid"
    limit: int = Field(default=5, strict=True, ge=1, le=8)
    budget_tokens: int = Field(default=4096, strict=True, ge=1024, le=8192)
    query_budget_tokens: int = Field(default=2048, strict=True, ge=256, le=4096)
