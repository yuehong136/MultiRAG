"""Pure, budgeted memory context assembled from authorized read snapshots.

This module performs no storage reads or writes. Source resolution establishes
provenance for this read only; it does not establish factual verification,
transactional revocation, or a persistent source version.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias, TypedDict, TypeGuard

SourceState = Literal["not_applicable", "missing", "inactive", "forgotten", "mismatch", "available"]
SourceKey: TypeAlias = tuple[str, str]
Message: TypeAlias = Mapping[str, Any]
SourceMessages: TypeAlias = Mapping[SourceKey, Message | None]
TokenCounter: TypeAlias = Callable[[str], int]

_SOURCE_STATES: tuple[SourceState, ...] = ("not_applicable", "missing", "inactive", "forgotten", "mismatch", "available")
_MESSAGE_TYPES = frozenset({"raw", "semantic", "episodic", "procedural"})
_IDENTITY_FIELDS = ("memory_id", "user_id", "agent_id", "session_id")
_MISSING = object()


@dataclass(frozen=True)
class ContextScope:
    """Already-authorized Memory IDs and optional exact content selectors."""

    memory_ids: frozenset[str]
    user_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.memory_ids, frozenset) or not self.memory_ids or any(not isinstance(value, str) or not value.strip() for value in self.memory_ids):
            raise ValueError("memory_ids must be a nonempty frozenset of Memory IDs")
        if any(value is not None and not isinstance(value, str) for value in (self.user_id, self.agent_id, self.session_id)):
            raise ValueError("Scope selectors must be strings or None")


@dataclass(frozen=True)
class ContextBudget:
    max_tokens: int
    max_items: int = 5
    source_excerpt_chars: int = 512

    def __post_init__(self) -> None:
        if type(self.max_tokens) is not int or self.max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        if type(self.max_items) is not int or not 1 <= self.max_items <= 100:
            raise ValueError("max_items must be an integer between 1 and 100")
        if type(self.source_excerpt_chars) is not int or not 0 <= self.source_excerpt_chars <= 10000:
            raise ValueError("source_excerpt_chars must be an integer between 0 and 10000")


class SourceLocator(TypedDict):
    kind: Literal["memory_message"]
    memory_id: str
    message_id: str


class SourceInfo(TypedDict):
    state: SourceState
    verification: Literal["provenance_only"]
    snapshot_only: bool
    locator: SourceLocator | None
    content_sha256: str | None
    excerpt: str | None
    offset: int | None
    next_offset: int | None
    total_chars: int | None
    truncated: bool
    continuation_requires_authorization: bool


class ContextItem(TypedDict):
    message_id: str
    memory_id: str
    message_type: str
    content: str
    user_id: str
    agent_id: str
    session_id: str
    valid_at: str | None
    invalid_at: str | None
    role_status: Literal["legacy_role_unknown"]
    source: SourceInfo


class ContextUsage(TypedDict):
    estimated_tokens: int
    budget_tokens: int
    token_count_is_estimate: bool


class QueryContext(TypedDict):
    items: list[ContextItem]
    usage: ContextUsage
    truncated: bool
    omitted_count: int
    deduplicated_count: int
    source_state_counts: dict[str, int]
    source_snapshot_only: bool


class ContextBudgetError(ValueError):
    """Even an empty context envelope cannot fit the requested estimate budget."""


class SourceContentChangedError(ValueError):
    """A stateless excerpt cursor no longer matches the source content."""


def message_in_scope(message: Message, scope: ContextScope) -> bool:
    """Check exact scope fields without treating selectors as authorization.

    Missing identity fields are not inferred from a parent, request, or text.
    Empty string identities remain legitimate legacy values when unconstrained.
    """
    if any(not _valid_text(message.get(field)) for field in _IDENTITY_FIELDS):
        return False
    if message["memory_id"] not in scope.memory_ids:
        return False
    return all(expected is None or message[field] == expected for field, expected in (("user_id", scope.user_id), ("agent_id", scope.agent_id), ("session_id", scope.session_id)))


def _message_id(value: object, *, zero: bool = False) -> str | None:
    # ES and OceanBase normalize a legitimate RAW source_id=0 to None.
    if zero and (value is None or value == ""):
        return "0"
    if type(value) is int:
        return str(value) if value >= (0 if zero else 1) else None
    if isinstance(value, str) and len(value) <= 32 and value.isascii() and value.isdecimal() and str(int(value)) == value and int(value) >= (0 if zero else 1):
        return value
    return None


def _valid_text(value: object) -> TypeGuard[str]:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _record_state(message: Message) -> SourceState:
    status = message.get("status", _MISSING)
    forgotten = message.get("forget_at", _MISSING)
    if not (type(status) is bool or (type(status) is int and status in (0, 1))):
        return "mismatch"
    if forgotten is _MISSING or forgotten == "-" or (forgotten is not None and not isinstance(forgotten, str)):
        return "mismatch"
    if forgotten:
        return "forgotten"
    return "available" if status else "inactive"


def _source_info(state: SourceState, locator: SourceLocator | None = None) -> SourceInfo:
    return {
        "state": state,
        "verification": "provenance_only",
        "snapshot_only": True,
        "locator": locator,
        "content_sha256": None,
        "excerpt": None,
        "offset": None,
        "next_offset": None,
        "total_chars": None,
        "truncated": False,
        "continuation_requires_authorization": True,
    }


def _locator(message: Message) -> SourceLocator | None:
    identity = _message_id(message.get("message_id"))
    memory_id = message.get("memory_id")
    if identity is None or not isinstance(memory_id, str):
        return None
    return {"kind": "memory_message", "memory_id": memory_id, "message_id": identity}


def build_source_excerpt(
    source: Message,
    *,
    scope: ContextScope,
    offset: int = 0,
    limit: int = 512,
    content_sha256: str | None = None,
) -> SourceInfo:
    """Build a code-point cursor after validating a current RAW read snapshot.

    The host must authorize and re-read the source on every continuation. Hashes
    detect changed content, not permissions, source revisions, or revocation.
    """
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 0 <= limit <= 10000:
        raise ValueError("offset must be nonnegative and limit between 0 and 10000")
    if not message_in_scope(source, scope) or source.get("message_type") != "raw" or _message_id(source.get("source_id", _MISSING), zero=True) != "0":
        return _source_info("mismatch")
    locator = _locator(source)
    if locator is None:
        return _source_info("mismatch")
    state = _record_state(source)
    if state != "available":
        return _source_info(state, locator)
    content = source.get("content")
    if not _valid_text(content):
        return _source_info("mismatch", locator)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if content_sha256 is not None and content_sha256 != digest:
        raise SourceContentChangedError("Source content changed; request a new excerpt")
    if offset > len(content):
        raise ValueError("offset exceeds the current source length")
    end = min(len(content), offset + limit)
    return {
        **_source_info("available", locator),
        "content_sha256": digest,
        "excerpt": content[offset:end],
        "offset": offset,
        "next_offset": end if end < len(content) else None,
        "total_chars": len(content),
        "truncated": end < len(content),
    }


def resolve_message_source(
    candidate: Message,
    sources: SourceMessages,
    *,
    scope: ContextScope,
    excerpt_chars: int = 512,
    include_excerpt: bool = True,
) -> SourceInfo:
    """Resolve one real RAW parent without returning mismatched source text."""
    if not message_in_scope(candidate, scope) or _locator(candidate) is None or candidate.get("message_type") not in _MESSAGE_TYPES:
        return _source_info("mismatch")
    source_id = _message_id(candidate.get("source_id", _MISSING), zero=True)
    if candidate["message_type"] == "raw":
        return _source_info("not_applicable" if source_id == "0" else "mismatch")
    if source_id is None or source_id == "0" or source_id == _message_id(candidate["message_id"]):
        return _source_info("mismatch")
    locator: SourceLocator = {"kind": "memory_message", "memory_id": candidate["memory_id"], "message_id": source_id}
    source = sources.get((candidate["memory_id"], source_id))
    if source is None:
        return _source_info("missing", locator)
    if any(source.get(field, _MISSING) != candidate[field] for field in _IDENTITY_FIELDS) or _message_id(source.get("message_id")) != source_id:
        return _source_info("mismatch", locator)
    result = build_source_excerpt(source, scope=scope, limit=excerpt_chars)
    if not include_excerpt and result["state"] == "available":
        result = {**result, "excerpt": None, "offset": None, "next_offset": None, "truncated": False}
    return result


def serialize_query_context(context: QueryContext) -> str:
    """Serialize the complete domain envelope measured by the supplied estimator."""
    return json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _measured_context(context: QueryContext, count_tokens: TokenCounter) -> QueryContext:
    # Include the token estimate's own serialized field. Use a conservative
    # fixed point rather than omitting metadata or repeatedly shrinking text.
    for _ in range(64):
        measured = count_tokens(serialize_query_context(context))
        if type(measured) is not int or measured < 0:
            raise ValueError("Token estimator must return a nonnegative integer")
        if measured <= context["usage"]["estimated_tokens"]:
            return context
        context["usage"]["estimated_tokens"] = measured
        if measured > context["usage"]["budget_tokens"]:
            return context
    raise ValueError("Token estimator did not converge for the context envelope")


def assemble_query_context(
    candidates: Sequence[Message],
    sources: SourceMessages,
    *,
    scope: ContextScope,
    budget: ContextBudget,
    count_tokens: TokenCounter,
    include_sources: bool = True,
) -> QueryContext:
    """Select whole records in retrieval order within an estimated JSON budget.

    Exact duplicate items are removed only after an identical item is selected.
    Different source identities, times, or scopes are never text-deduplicated.
    HTTP envelopes and caller prompt templates are outside this domain budget.
    """
    state_counts: dict[str, int] = dict.fromkeys(_SOURCE_STATES, 0)
    prepared: list[tuple[int, ContextItem, str]] = []
    for position, candidate in enumerate(candidates):
        source = resolve_message_source(candidate, sources, scope=scope, excerpt_chars=budget.source_excerpt_chars, include_excerpt=include_sources)
        state_counts[source["state"]] += 1
        if source["state"] not in {"available", "not_applicable"} or _record_state(candidate) != "available" or not _valid_text(candidate.get("content")):
            continue
        item: ContextItem = {
            "message_id": str(candidate["message_id"]),
            "memory_id": candidate["memory_id"],
            "message_type": candidate["message_type"],
            "content": candidate["content"],
            "user_id": candidate["user_id"],
            "agent_id": candidate["agent_id"],
            "session_id": candidate["session_id"],
            "valid_at": candidate.get("valid_at") if _valid_text(candidate.get("valid_at")) else None,
            "invalid_at": candidate.get("invalid_at") if _valid_text(candidate.get("invalid_at")) else None,
            "role_status": "legacy_role_unknown",
            "source": source,
        }
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        prepared.append((position, item, key))

    def render(selected: list[tuple[int, ContextItem, str]]) -> QueryContext:
        selected_positions = {position for position, _, _ in selected}
        first_selected = {key: position for position, _, key in selected}
        duplicates = sum(position not in selected_positions and key in first_selected and position > first_selected[key] for position, _, key in prepared)
        omitted_eligible = len(prepared) - len(selected) - duplicates
        context: QueryContext = {
            "items": [item for _, item, _ in selected],
            "usage": {"estimated_tokens": 0, "budget_tokens": budget.max_tokens, "token_count_is_estimate": True},
            "truncated": omitted_eligible > 0,
            "omitted_count": len(candidates) - len(selected) - duplicates,
            "deduplicated_count": duplicates,
            "source_state_counts": dict(state_counts),
            "source_snapshot_only": True,
        }
        return _measured_context(context, count_tokens)

    selected: list[tuple[int, ContextItem, str]] = []
    result = render(selected)
    if result["usage"]["estimated_tokens"] > budget.max_tokens:
        raise ContextBudgetError("The estimated context envelope exceeds max_tokens")
    selected_keys: set[str] = set()
    for record in prepared:
        if record[2] in selected_keys or len(selected) >= budget.max_items:
            continue
        proposed = render([*selected, record])
        if proposed["usage"]["estimated_tokens"] <= budget.max_tokens:
            selected.append(record)
            selected_keys.add(record[2])
            result = proposed
    return result
