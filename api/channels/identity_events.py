"""Provider-neutral identity events emitted by managed Channel runtimes."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type ChannelIdentityEventType = Literal[
    "contact.user.created_v3",
    "contact.user.updated_v3",
    "contact.user.deleted_v3",
    "contact.scope.updated_v3",
]
type ChannelIdentityIdentifierKind = Literal["open_id", "user_id", "union_id"]
type ChannelDirectoryStatus = Literal["active", "inactive", "unknown"]

USER_IDENTITY_EVENT_TYPES = frozenset(
    {
        "contact.user.created_v3",
        "contact.user.updated_v3",
        "contact.user.deleted_v3",
    }
)
_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")


def _valid_opaque_text(value: str, *, max_length: int) -> bool:
    return bool(value) and len(value) <= max_length and value == value.strip() and all(character.isprintable() for character in value)


class ChannelIdentityIdentifier(BaseModel):
    """One allowlisted provider identifier; its value is never shown in repr."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ChannelIdentityIdentifierKind
    value: str = Field(min_length=1, max_length=255, repr=False)

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        if not _valid_opaque_text(value, max_length=255):
            raise ValueError("identity identifier is invalid")
        return value


class ChannelIdentitySubject(BaseModel):
    """Minimal user projection; directory PII and scope membership are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identifiers: tuple[ChannelIdentityIdentifier, ...] = Field(min_length=1, max_length=3)
    directory_status: ChannelDirectoryStatus

    @model_validator(mode="after")
    def _validate_identifiers(self) -> ChannelIdentitySubject:
        kinds = [identifier.kind for identifier in self.identifiers]
        if len(kinds) != len(set(kinds)):
            raise ValueError("identity identifier kinds must be unique")
        return self


class ChannelIdentityEvent(BaseModel):
    """Bounded worker-to-API identity event with no tenant/account authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    event_type: ChannelIdentityEventType
    event_id: str = Field(min_length=1, max_length=255, repr=False)
    event_at: datetime
    observed_app_id: str = Field(min_length=1, max_length=255, repr=False)
    observed_tenant_key: str = Field(min_length=1, max_length=255, repr=False)
    subject: ChannelIdentitySubject | None = Field(default=None, repr=False)

    @field_validator("event_id")
    @classmethod
    def _validate_event_id(cls, value: str) -> str:
        if not _EVENT_ID_PATTERN.fullmatch(value):
            raise ValueError("identity event ID is invalid")
        return value

    @field_validator("observed_app_id", "observed_tenant_key")
    @classmethod
    def _validate_opaque_header_value(cls, value: str) -> str:
        if not _valid_opaque_text(value, max_length=255):
            raise ValueError("identity event header is invalid")
        return value

    @field_validator("event_at", mode="before")
    @classmethod
    def _validate_event_at_input(cls, value: object) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("identity event time is invalid")
        return value

    @field_validator("event_at")
    @classmethod
    def _normalize_event_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("identity event time must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_subject_shape(self) -> ChannelIdentityEvent:
        if self.event_type in USER_IDENTITY_EVENT_TYPES:
            if self.subject is None:
                raise ValueError("user identity event requires a subject")
        elif self.subject is not None:
            raise ValueError("scope identity event must not enumerate users")
        return self


type IdentityEventHandler = Callable[[ChannelIdentityEvent], Awaitable[None]]


@runtime_checkable
class IdentityEventChannel(Protocol):
    """Optional Channel capability for durable provider directory events."""

    def set_identity_event_handler(self, handler: IdentityEventHandler) -> None: ...
