"""Bounded callback envelopes shared by API and worker; no transport SDK."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CallbackMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    message_id: str = Field(min_length=1, max_length=255)
    chat_id: str = Field(min_length=1, max_length=255)
    sender_id: str = Field(min_length=1, max_length=255)
    chat_type: Literal["p2p", "group"]
    text: str = Field(min_length=1, max_length=16_000)


class CallbackReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt: str = Field(pattern=r"^[a-f0-9]{64}$")
    message: CallbackMessage


class CallbackBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[CallbackReceipt] = Field(default_factory=list, max_length=1)


class CallbackAck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt: str = Field(pattern=r"^[a-f0-9]{64}$")
