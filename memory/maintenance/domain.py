"""Deterministic validation and host assembly of unverified maintenance proposals."""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from memory.maintenance.segments import validate_fragment
from memory.revisions.domain import RevisionConflict, RevisionValidationError, content_hash, positive_int, request_hash, revised_content

MAX_SOURCES = 32
MAX_CANDIDATES = 32
ASSESSMENT = {"kind": "hypothesis", "verification_status": "unverified"}
POLICIES = {"general", "preferences", "decisions", "procedures"}


def identifier(value: Any, *, maximum: int = 32) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value) or len(value) > maximum:
        raise RevisionValidationError("invalid_maintenance_identifier")
    return value


def canonical_request(source_ids: list[int], entry_ids: list[str], policy: str, input_budget_tokens: int, output_budget_tokens: int) -> dict[str, Any]:
    if not isinstance(source_ids, list) or not 1 <= len(source_ids) <= MAX_SOURCES:
        raise RevisionValidationError("invalid_maintenance_sources")
    sources = sorted(positive_int(item, "invalid_source_id") for item in source_ids)
    if len(set(sources)) != len(sources):
        raise RevisionValidationError("invalid_maintenance_sources")
    if not isinstance(entry_ids, list) or len(entry_ids) > MAX_SOURCES:
        raise RevisionValidationError("invalid_maintenance_entries")
    entries = sorted(identifier(item) for item in entry_ids)
    if len(set(entries)) != len(entries):
        raise RevisionValidationError("invalid_maintenance_entries")
    if not isinstance(policy, str) or policy not in POLICIES:
        raise RevisionValidationError("invalid_maintenance_policy")
    if type(input_budget_tokens) is not int or not 1024 <= input_budget_tokens <= 32768 or type(output_budget_tokens) is not int or not 256 <= output_budget_tokens <= 8192:
        raise RevisionValidationError("invalid_maintenance_budget")
    return {"source_ids": sources, "entry_ids": entries, "policy": policy, "input_budget_tokens": input_budget_tokens, "output_budget_tokens": output_budget_tokens}


def _scope(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"user_id", "agent_id", "session_id"} or any(not isinstance(item, str) or len(item.encode("utf-8")) > 256 for item in value.values()):
        raise RevisionValidationError("invalid_maintenance_scope")
    return dict(value)


def _hash(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RevisionValidationError("invalid_maintenance_content_hash")
    return value


def _text(value: Any, count_tokens: Callable[[str], int], *, body: bool = True) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > (65535 if body else 4096):
        raise RevisionValidationError("invalid_maintenance_content")
    count = count_tokens(value)
    if type(count) is not int or not 0 <= count <= (8192 if body else 2048):
        raise RevisionValidationError("maintenance_content_budget_exceeded")
    return value


def _snapshot_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 65535:
        raise RevisionValidationError("invalid_maintenance_snapshot_content")
    return value


def validate_manifest(manifest: dict[str, Any], request: dict[str, Any], count_tokens: Callable[[str], int]) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != {"inputs", "entries", "estimated_input_tokens"}:
        raise RevisionValidationError("invalid_maintenance_manifest")
    inputs, entries = manifest["inputs"], manifest["entries"]
    if not isinstance(inputs, list) or not isinstance(entries, list) or len(inputs) != len(request["source_ids"]) or len(entries) != len(request["entry_ids"]):
        raise RevisionValidationError("maintenance_manifest_scope_mismatch")
    input_ids, sources = set(), set()
    for item in inputs:
        if not isinstance(item, dict) or set(item) - {"fragment"} != {
            "input_id",
            "source_id",
            "source_revision",
            "source_generation",
            "content_sha256",
            "scope",
            "valid_at",
            "snapshot",
            "coverage",
            "defer_reason",
        }:
            raise RevisionValidationError("invalid_maintenance_input")
        input_id = identifier(item["input_id"])
        source_id = positive_int(item["source_id"], "invalid_source_id")
        if input_id in input_ids or source_id in sources:
            raise RevisionValidationError("duplicate_maintenance_input")
        input_ids.add(input_id)
        sources.add(source_id)
        positive_int(item["source_revision"])
        positive_int(item["source_generation"])
        _hash(item["content_sha256"])
        _scope(item["scope"])
        if item["valid_at"] is not None and (not isinstance(item["valid_at"], str) or len(item["valid_at"]) > 64):
            raise RevisionValidationError("invalid_maintenance_reference_time")
        if item["coverage"] == "included":
            snapshot = item["snapshot"]
            if not isinstance(snapshot, dict) or snapshot.get("message_type") != "raw":
                raise RevisionValidationError("invalid_maintenance_origin")
            if snapshot.get("origin") == "authenticated_submission":
                from memory.structured_sources import validate_submission_metadata

                if set(snapshot) != {"content", "message_type", "origin", "role_status", "source_metadata"} or snapshot.get("role_status") != "caller_declared":
                    raise RevisionValidationError("invalid_maintenance_origin")
                checked = validate_submission_metadata(snapshot["source_metadata"])
                if checked["envelope_sha256"] != item["content_sha256"]:
                    raise RevisionValidationError("invalid_maintenance_origin")
            elif set(snapshot) != {"content", "message_type", "origin", "role_status"} or snapshot.get("origin") != "legacy_combined" or snapshot.get("role_status") != "role_unknown":
                raise RevisionValidationError("invalid_maintenance_origin")
            if item.get("fragment") is not None:
                if not isinstance(snapshot.get("content"), str) or not snapshot["content"] or len(snapshot["content"].encode("utf-8")) > 65535:
                    raise RevisionValidationError("invalid_maintenance_snapshot_content")
                validate_fragment(item["fragment"], content=snapshot["content"])
            elif content_hash(_snapshot_text(snapshot["content"])) != item["content_sha256"]:
                raise RevisionConflict("maintenance_input_hash_mismatch")
            if item["defer_reason"] is not None:
                raise RevisionValidationError("invalid_maintenance_coverage")
        elif item["coverage"] == "deferred":
            if item["snapshot"] != {} or not isinstance(item["defer_reason"], str) or not re.fullmatch(r"[a-z_]{1,64}", item["defer_reason"]):
                raise RevisionValidationError("invalid_maintenance_coverage")
        else:
            raise RevisionValidationError("invalid_maintenance_coverage")
    if sources != set(request["source_ids"]):
        raise RevisionValidationError("maintenance_manifest_scope_mismatch")
    seen_entries = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not {"entry_id", "revision", "message_id", "generation", "content", "content_sha256", "scope", "source_ids"} <= set(entry)
            or set(entry) - {"entry_id", "revision", "message_id", "generation", "content", "content_sha256", "scope", "source_ids", "message_type"}
        ):
            raise RevisionValidationError("invalid_maintenance_target")
        entry_id = identifier(entry["entry_id"])
        if entry_id in seen_entries:
            raise RevisionValidationError("duplicate_maintenance_target")
        seen_entries.add(entry_id)
        for field in ("revision", "message_id", "generation"):
            positive_int(entry[field])
        if _hash(entry["content_sha256"]) != content_hash(_snapshot_text(entry["content"])):
            raise RevisionConflict("maintenance_target_hash_mismatch")
        _scope(entry["scope"])
        if not isinstance(entry["source_ids"], list) or not entry["source_ids"] or any(type(value) is not int or value not in sources for value in entry["source_ids"]):
            raise RevisionConflict("maintenance_target_sources_out_of_scope")
    if seen_entries != set(request["entry_ids"]):
        raise RevisionValidationError("maintenance_manifest_scope_mismatch")
    estimate = manifest["estimated_input_tokens"]
    measured = count_tokens(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    if type(estimate) is not int or type(measured) is not int or not 0 <= estimate <= request["input_budget_tokens"] or measured < 0 or measured > request["input_budget_tokens"]:
        raise RevisionValidationError("maintenance_input_budget_exceeded")
    return deepcopy(manifest)


def assemble_candidates(proposals: list[dict[str, Any]], manifest: dict[str, Any], count_tokens: Callable[[str], int], output_budget_tokens: int) -> list[dict[str, Any]]:
    if not isinstance(proposals, list) or len(proposals) > MAX_CANDIDATES:
        raise RevisionValidationError("invalid_maintenance_proposals")
    raw_tokens = count_tokens(json.dumps(proposals, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    if type(raw_tokens) is not int or not 0 <= raw_tokens <= output_budget_tokens:
        raise RevisionValidationError("maintenance_output_budget_exceeded")
    inputs = {item["input_id"]: item for item in manifest["inputs"]}
    entries = {entry["entry_id"]: entry for entry in manifest["entries"]}
    result, fingerprints = [], set()
    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise RevisionValidationError("invalid_maintenance_proposal")
        action = proposal.get("action")
        fields = {
            "add": {"action", "content", "message_type", "source_input_ids", "reason"},
            "patch": {"action", "target_entry_id", "patch", "source_input_ids", "reason"},
            "noop": {"action", "reason", "source_input_ids"},
        }
        if not isinstance(action, str) or action not in fields or set(proposal) != fields[action]:
            raise RevisionValidationError("invalid_maintenance_action_fields")
        references = proposal["source_input_ids"]
        if (
            not isinstance(references, list)
            or not references
            or any(not isinstance(item, str) or item not in inputs or inputs[item]["coverage"] not in {"included", "analyzed"} for item in references)
            or len(set(references)) != len(references)
        ):
            raise RevisionValidationError("maintenance_evidence_out_of_scope")
        selected = [inputs[item] for item in sorted(references)]
        principals = {(item["scope"]["user_id"], item["scope"]["agent_id"]) for item in selected}
        if len(principals) != 1:
            raise RevisionValidationError("maintenance_evidence_principal_mismatch")
        reason = _text(proposal["reason"], count_tokens, body=False)
        candidate: dict[str, Any] = {
            "action": action,
            "target_entry_id": None,
            "expected_revision": None,
            "message_type": None,
            "content": None,
            "base_content": None,
            "base_content_sha256": None,
            "patch": None,
            "diff": None,
            "source_input_ids": sorted(references),
            "sources": [{key: item[key] for key in ("input_id", "source_id", "source_revision", "source_generation", "content_sha256")} for item in selected],
            "reason": reason,
            "assessment": dict(ASSESSMENT),
        }
        for reference, original in zip(candidate["sources"], selected, strict=True):
            if original.get("fragment") is not None:
                reference["fragment"] = deepcopy(original["fragment"])
            if original["snapshot"].get("source_metadata") is not None:
                reference["source_provenance_sha256"] = original["snapshot"]["source_metadata"]["provenance_sha256"]
        if action == "add":
            if not isinstance(proposal["message_type"], str) or proposal["message_type"] not in {"semantic", "episodic", "procedural"}:
                raise RevisionValidationError("invalid_maintenance_message_type")
            candidate.update(content=_text(proposal["content"], count_tokens), message_type=proposal["message_type"])
        elif action == "patch":
            target = entries.get(proposal["target_entry_id"]) if isinstance(proposal["target_entry_id"], str) else None
            if target is None:
                raise RevisionValidationError("maintenance_target_out_of_scope")
            if (target["scope"]["user_id"], target["scope"]["agent_id"]) not in principals or not set(target["source_ids"]) <= {item["source_id"] for item in selected}:
                raise RevisionValidationError("maintenance_target_evidence_mismatch")
            content = revised_content(target["content"], content=None, patch=proposal["patch"], count_tokens=count_tokens)
            final = content[0]
            candidate.update(
                target_entry_id=target["entry_id"],
                expected_revision=target["revision"],
                message_type=target.get("message_type"),
                content=final,
                base_content=target["content"],
                base_content_sha256=target["content_sha256"],
                patch=deepcopy(proposal["patch"]),
                diff="\n".join(difflib.unified_diff(target["content"].splitlines(), final.splitlines(), fromfile="before", tofile="after", lineterm="")),
            )
        fingerprint = request_hash(candidate)
        if fingerprint in fingerprints:
            raise RevisionValidationError("duplicate_maintenance_proposal")
        fingerprints.add(fingerprint)
        result.append(candidate)
    return result
