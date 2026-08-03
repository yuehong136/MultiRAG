"""Strict private-API models for independent Channel runtimes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DesiredRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_id: str = Field(min_length=1, max_length=32)
    provider: Literal["feishu"]
    generation: int = Field(ge=1)


class DesiredRuntimeList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[DesiredRuntime]


class RuntimeCredential(BaseModel):
    """Credential returned only through the authenticated private route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    app_id: str = Field(min_length=1, max_length=128)
    app_secret: str = Field(min_length=1, json_schema_extra={"writeOnly": True})


class RuntimeBindingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_id: str = Field(min_length=1, max_length=32)
    provider: Literal["feishu"]
    generation: int = Field(ge=1)
    public_config: dict[str, Any]
    credential: RuntimeCredential


RuntimeState = Literal["waiting", "starting", "connected", "stopping", "stopped", "error"]


class RuntimeReport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    observed_generation: int = Field(ge=0)
    state: RuntimeState
    runner_id: str | None = Field(default=None, min_length=1, max_length=128)
    connected_at: datetime | None = None
    last_error_code: str | None = Field(default=None, min_length=1, max_length=64)
