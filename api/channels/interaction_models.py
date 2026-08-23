"""Strict private-wire DTOs for Channel interaction delivery."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class ChannelInteractionFormOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1, max_length=64, repr=False)
    label: str = Field(min_length=1, max_length=80)


class ChannelInteractionFormField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64, repr=False)
    kind: Literal[
        "text",
        "number",
        "boolean",
        "date",
        "select",
        "multi_select",
    ]
    label: str = Field(min_length=1, max_length=80)
    required: bool = False
    options: tuple[ChannelInteractionFormOption, ...] = Field(
        default=(),
        max_length=20,
    )
    min_length: int | None = Field(default=None, ge=0, le=1000)
    max_length: int | None = Field(default=None, ge=1, le=1000)
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def validate_kind_shape(self) -> ChannelInteractionFormField:
        if self.kind in {"select", "multi_select", "boolean"}:
            if not self.options:
                raise ValueError("selection fields require options")
        elif self.options:
            raise ValueError("non-selection fields cannot carry options")
        if self.kind in {"text", "multi_select"}:
            if self.min_length is None or self.max_length is None or self.min_length > self.max_length or self.minimum is not None or self.maximum is not None:
                raise ValueError("text or selection bounds are invalid")
        elif self.kind in {"date", "select"}:
            if (
                (self.min_length is None) != (self.max_length is None)
                or (self.min_length is not None and self.max_length is not None and self.min_length > self.max_length)
                or self.minimum is not None
                or self.maximum is not None
            ):
                raise ValueError("text or selection bounds are invalid")
        elif self.min_length is not None or self.max_length is not None:
            raise ValueError("numeric bounds are invalid")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("numeric bounds are invalid")
        values = [option.value for option in self.options]
        if len(values) != len(set(values)):
            raise ValueError("interaction options must be unique")
        return self


class ChannelInteractionFormProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message: str = Field(min_length=1, max_length=240)
    fields: tuple[ChannelInteractionFormField, ...] = Field(
        min_length=1,
        max_length=12,
    )


class ChannelInteractionTerminalProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["completed", "declined", "cancelled", "expired", "failed"]
    message: str = Field(min_length=1, max_length=240)


class ClaimedInteractionDelivery(BaseModel):
    """One leased delivery. Secrets are hidden from repr and never logged."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    delivery_id: str = Field(min_length=1, max_length=32, repr=False)
    delivery_token: SecretStr = Field(min_length=16, max_length=255, repr=False)
    action_id: str = Field(min_length=1, max_length=32, repr=False)
    revision: int = Field(gt=0)
    kind: Literal["form", "terminal"]
    presentation_ref: str = Field(min_length=1, max_length=255, repr=False)
    projection: ChannelInteractionFormProjection | ChannelInteractionTerminalProjection
    action_nonce: SecretStr | None = Field(
        default=None,
        min_length=16,
        max_length=255,
        repr=False,
    )
    expires_at: datetime
    safe_error_code: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{0,63}$",
    )

    @model_validator(mode="after")
    def validate_delivery_shape(self) -> ClaimedInteractionDelivery:
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("interaction expiration must be timezone-aware")
        if self.kind == "form":
            if self.action_nonce is None or not isinstance(self.projection, ChannelInteractionFormProjection):
                raise ValueError("form delivery is invalid")
        elif self.action_nonce is not None or not isinstance(
            self.projection,
            ChannelInteractionTerminalProjection,
        ):
            raise ValueError("terminal delivery is invalid")
        return self


class InteractionCallbackReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["accepted", "duplicate"]


__all__ = [
    "ChannelInteractionFormField",
    "ChannelInteractionFormOption",
    "ChannelInteractionFormProjection",
    "ChannelInteractionTerminalProjection",
    "ClaimedInteractionDelivery",
    "InteractionCallbackReceipt",
]
