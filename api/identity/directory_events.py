"""Framework-neutral contracts for durable provider directory events."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from api.identity.contracts import ProviderContext
from api.identity.providers.contracts import ProviderIdentifierKind

_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")


class DirectoryEventType(StrEnum):
    USER_CREATED = "contact.user.created_v3"
    USER_UPDATED = "contact.user.updated_v3"
    USER_DELETED = "contact.user.deleted_v3"
    SCOPE_UPDATED = "contact.scope.updated_v3"


class DirectoryStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class DirectoryEventOutcome(StrEnum):
    APPLIED = "applied"
    STALE = "stale"
    DUPLICATE = "duplicate"
    NO_LINK = "no_link"
    FAILED = "failed"


class DirectoryEventErrorCode(StrEnum):
    INVALID = "IDENTITY_DIRECTORY_EVENT_INVALID"
    BINDING_NOT_FOUND = "IDENTITY_DIRECTORY_BINDING_NOT_FOUND"
    BINDING_DISABLED = "IDENTITY_DIRECTORY_BINDING_DISABLED"
    AUTHORITY_INVALID = "IDENTITY_DIRECTORY_AUTHORITY_INVALID"
    REPLAY_CONFLICT = "IDENTITY_DIRECTORY_REPLAY_CONFLICT"
    RECEIPT_FAILED = "IDENTITY_DIRECTORY_RECEIPT_FAILED"
    RECEIPT_PROCESSING = "IDENTITY_DIRECTORY_RECEIPT_PROCESSING"
    IDENTITY_CONFLICT = "IDENTITY_DIRECTORY_IDENTITY_CONFLICT"
    REPOSITORY_UNAVAILABLE = "IDENTITY_DIRECTORY_REPOSITORY_UNAVAILABLE"


class DirectoryEventError(RuntimeError):
    """Classified event failure that never includes provider identifiers."""

    def __init__(self, code: DirectoryEventErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class DirectoryEventIdentifier:
    kind: ProviderIdentifierKind
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, ProviderIdentifierKind)
            or type(self.value) is not str
            or not self.value
            or self.value != self.value.strip()
            or len(self.value) > 255
            or not all(character.isprintable() for character in self.value)
        ):
            raise ValueError("directory event identifier is invalid")


@dataclass(frozen=True, slots=True)
class DirectoryEventCommand:
    binding_id: str = field(repr=False)
    binding_generation: int
    event_type: DirectoryEventType
    event_id: str = field(repr=False)
    event_at: datetime
    observed_app_id: str = field(repr=False)
    observed_tenant_key: str = field(repr=False)
    identifiers: tuple[DirectoryEventIdentifier, ...] = field(default=(), repr=False)
    directory_status: DirectoryStatus | None = None

    def __post_init__(self) -> None:
        opaque_values = (
            (self.binding_id, 32),
            (self.observed_app_id, 255),
            (self.observed_tenant_key, 255),
        )
        if any(type(value) is not str or not value or value != value.strip() or len(value) > limit or not all(character.isprintable() for character in value) for value, limit in opaque_values):
            raise ValueError("directory event command is invalid")
        if type(self.event_id) is not str or not _EVENT_ID_PATTERN.fullmatch(self.event_id):
            raise ValueError("directory event command is invalid")
        if type(self.binding_generation) is not int or self.binding_generation < 1:
            raise ValueError("directory event command is invalid")
        if not isinstance(self.event_type, DirectoryEventType):
            raise ValueError("directory event command is invalid")
        if self.event_at.tzinfo is None or self.event_at.utcoffset() is None:
            raise ValueError("directory event command is invalid")
        kinds = [identifier.kind for identifier in self.identifiers]
        if len(kinds) != len(set(kinds)) or len(kinds) > 3:
            raise ValueError("directory event command is invalid")
        is_scope = self.event_type is DirectoryEventType.SCOPE_UPDATED
        if is_scope != (not self.identifiers and self.directory_status is None):
            raise ValueError("directory event command is invalid")
        if not is_scope and (not self.identifiers or not isinstance(self.directory_status, DirectoryStatus)):
            raise ValueError("directory event command is invalid")


@dataclass(frozen=True, slots=True)
class DirectoryEventProcessingResult:
    outcome: DirectoryEventOutcome
    context: ProviderContext | None = field(default=None, repr=False)
    revision_bumped: bool = False
    error_code: DirectoryEventErrorCode | None = None

    def __post_init__(self) -> None:
        failed = self.outcome is DirectoryEventOutcome.FAILED
        if failed != (self.error_code is not None):
            raise ValueError("directory event result is invalid")
        if self.revision_bumped and self.context is None:
            raise ValueError("directory event result is invalid")
        if self.outcome is not DirectoryEventOutcome.NO_LINK and self.context is None:
            raise ValueError("directory event result is invalid")


@runtime_checkable
class DirectoryEventRepository(Protocol):
    """Narrow durable transaction port consumed by event adapters."""

    async def process(
        self,
        command: DirectoryEventCommand,
    ) -> DirectoryEventProcessingResult: ...


def canonical_directory_event_hash(command: DirectoryEventCommand) -> str:
    """Hash the normalized event and observed header proof, never raw payload."""

    subject: dict[str, object] | None = None
    if command.event_type is not DirectoryEventType.SCOPE_UPDATED:
        subject = {
            "directory_status": command.directory_status.value if command.directory_status is not None else None,
            "identifiers": [{"kind": identifier.kind.value, "value": identifier.value} for identifier in sorted(command.identifiers, key=lambda item: item.kind.value)],
        }
    payload = {
        "event_at": command.event_at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "event_id": command.event_id,
        "event_type": command.event_type.value,
        "observed_app_id": command.observed_app_id,
        "observed_tenant_key": command.observed_tenant_key,
        "subject": subject,
        "version": 1,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
