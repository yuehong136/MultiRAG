# Copyright 2025 Alibaba Group
# Licensed under the Apache License, Version 2.0; see the root LICENSE.
# Adapted from ReMe b13034ba2ff7339bb03df4fa4ee130b47a731c6c,
# reme/steps/evolve/dream/utils.py:130-179. Modified as described in NOTICE.
"""Deterministic metadata selection; no storage, models or source text reads.

The partition structure is adapted from ReMe ``batch_paths`` (Apache-2.0),
Copyright 2025 Alibaba Group. See NOTICE in this directory and the root LICENSE.
Modified for trusted principal boundaries and complete source-count budgets.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any, Literal

from memory.revisions.domain import RevisionValidationError, positive_int, request_hash

MAX_DISCOVERY_SOURCES = 256
MAX_BATCH_SOURCES = 32
MAX_SUBMIT_BATCHES = 8
SCOPE_FIELDS = ("user_id", "agent_id", "session_id")
CoverageState = Literal["eligible", "covered", "inflight", "blocked"]


def canonical_scope(value: Any) -> dict[str, str | None]:
    if not isinstance(value, dict) or set(value) != set(SCOPE_FIELDS):
        raise RevisionValidationError("invalid_incremental_scope")
    if any(item is not None and (not isinstance(item, str) or len(item.encode("utf-8")) > 256) for item in value.values()):
        raise RevisionValidationError("invalid_incremental_scope")
    return {field: value[field] for field in SCOPE_FIELDS}


def source_identity(source: dict[str, Any]) -> dict[str, Any]:
    """The observed SQL version, never a timestamp high-water mark."""
    return {name: deepcopy(source[name]) for name in ("source_id", "source_revision", "source_generation", "content_sha256")}


def public_source(source: dict[str, Any]) -> dict[str, str]:
    return {name: str(value) for name, value in source_identity(source).items()}


def source_key(memory_id: str, profile_sha256: str, source: dict[str, Any]) -> str:
    return request_hash({"memory_id": memory_id, "profile_sha256": profile_sha256, **source_identity(source)})


def principal(source: dict[str, Any]) -> tuple[str, str]:
    scope = source.get("scope")
    if not isinstance(scope, dict) or any(not isinstance(scope.get(name), str) for name in SCOPE_FIELDS):
        raise RevisionValidationError("invalid_incremental_source_scope")
    return scope["user_id"], scope["agent_id"]


def effective_coverage(
    *,
    ledger_state: str,
    job_state: str | None,
    execution_state: str | None,
    admission_exists: bool,
    actual_token_present: bool,
    source_matches: bool,
) -> bool:
    """A saved model result alone is not confirmed, current coverage."""
    return ledger_state == "analyzed" and job_state == "ready" and execution_state == "succeeded" and admission_exists and not actual_token_present and source_matches


def select_sources(sources: list[dict[str, Any]], states: dict[int, CoverageState], *, max_sources: int) -> dict[str, Any]:
    """Select eligible versions after status filtering, including late arrivals.

    This pure equivalent is also used by quality fixtures. SQL performs the
    same filtering before LIMIT; it does not materialize every source here.
    """
    if type(max_sources) is not int or not 1 <= max_sources <= MAX_DISCOVERY_SOURCES:
        raise RevisionValidationError("invalid_incremental_source_limit")
    counts = dict.fromkeys(("active", "covered", "inflight", "blocked", "eligible"), 0)
    eligible = []
    seen = set()
    for source in sorted(sources, key=lambda row: row["source_id"]):
        identity = positive_int(source["source_id"])
        if identity in seen:
            raise RevisionValidationError("duplicate_incremental_source")
        seen.add(identity)
        if source.get("state", "active") != "active":
            continue
        state = states.get(identity, "eligible")
        if state not in {"eligible", "covered", "inflight", "blocked"}:
            raise RevisionValidationError("invalid_incremental_coverage_state")
        counts["active"] += 1
        counts[state] += 1
        if state == "eligible":
            eligible.append(source)
    return {"sources": deepcopy(eligible[:max_sources]), "counts": counts, "has_more": len(eligible) > max_sources}


def partition_sources(
    primary_sources: list[dict[str, Any]],
    context_sources: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    *,
    batch_size: int,
) -> list[dict[str, Any]]:
    """Partition every primary once, repeating only explicit matching context.

    ReMe's stable group/flush partition is adapted here: groups are resolved
    SQL user+agent identities instead of filesystem days; capacity counts the
    full source-ID union, never byte estimates or truncated leading text.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SOURCES:
        raise RevisionValidationError("invalid_incremental_batch_size")
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    contexts: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    targets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for source in sorted(primary_sources, key=lambda row: row["source_id"]):
        groups[principal(source)].append(source)
    for source in sorted(context_sources, key=lambda row: row["source_id"]):
        contexts[principal(source)].append(source)
    for entry in sorted(entries, key=lambda row: row["entry_id"]):
        targets[principal(entry)].append(entry["entry_id"])
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for group in sorted(groups.values(), key=lambda rows: rows[0]["source_id"]):
        key = principal(group[0])
        explicit = contexts.get(key, [])
        context_ids = {row["source_id"] for row in explicit}
        if len(context_ids) > batch_size:
            raise RevisionValidationError("incremental_context_exhausts_batch")
        current: list[dict[str, Any]] = []

        def flush() -> None:
            if not current:
                return
            ids = {row["source_id"] for row in current}
            result.append(
                {
                    "ordinal": len(result),
                    "primary_sources": deepcopy(current),
                    "context_sources": deepcopy([row for row in explicit if row["source_id"] not in ids]),
                    "entry_ids": list(targets.get(key, [])),
                }
            )

        for source in group:
            identity = source["source_id"]
            if identity in seen:
                raise RevisionValidationError("duplicate_incremental_source")
            seen.add(identity)
            if len(context_ids | {row["source_id"] for row in current} | {identity}) > batch_size:
                flush()
                current = []
            if len(context_ids | {identity}) > batch_size:
                raise RevisionValidationError("incremental_context_exhausts_batch")
            current.append(source)
        flush()
    return result
