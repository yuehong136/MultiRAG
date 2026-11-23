"""Synthetic complete DTOs; never assert they are current server observations."""

from typing import Any

from memory.compaction.domain import input_fingerprint, package_for
from memory.revisions.domain import content_hash


def synthetic_artifact() -> tuple[dict[str, str], dict[str, Any]]:
    roots = []
    for identity in (str(2**63 - 1), "3"):
        text = "仅合成资料，保留0与false及[]。🙂" + identity
        binding = {
            "source_id": identity,
            "source_revision": "1",
            "source_generation": "1",
            "content_sha256": content_hash(text),
            "scope": {"user_id": "u", "agent_id": "a", "session_id": identity},
            "locator": {"kind": "memory_message", "memory_id": "bank", "message_id": identity},
            "provenance_sha256": None,
            "format": "legacy_combined",
            "role_status": "role_unknown",
            "reference_time": "2026-11-17 10:00:00",
            "event_time": {"value": None, "status": "unknown"},
        }
        roots.append({"binding": binding, "content": text})
    ref = {"memory_id": "bank", "operation_id": "original-op", "artifact_id": "artifact", "input_manifest_sha256": input_fingerprint(roots)}
    result = {
        "operation": {
            "memory_id": "bank",
            "operation_id": "original-op",
            "artifact_id": "artifact",
            "kind": "context_compact",
            "status": "generated",
            "writer_busy": False,
            "needs_operator_reconciliation": False,
        },
        "artifact": {
            **ref,
            "state": "generated",
            "validity_checked": True,
            "valid_for_current": True,
            "redacted": False,
            "snapshot_only": True,
            "sources": [root["binding"] for root in roots],
            "package": package_for(roots, prefix_count=1, summary="仅合成资料，0、false、[]须保留，实际执行未知。"),
        },
    }
    return ref, result


def consumer_options() -> dict[str, Any]:
    return {
        "scope": {"user_id": "u", "agent_id": "a", "session_id": None},
        "model": "gpt-6.1-terra",
        "question": "有哪些限制？",
        "context_window_tokens": 16384,
        "output_reserve_tokens": 512,
        "safety_margin_tokens": 256,
        "count_tokens": lambda text: len(text),
        "tokenizer_label": "codepoint_estimate_only",
    }
