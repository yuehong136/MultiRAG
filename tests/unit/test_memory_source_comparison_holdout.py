"""Sealed paired inputs exercise real contracts, never a semantic keyword grader.

The rubric is for independent review of provider outputs. Host-valid controlled
proposals below do not establish that a model extracted the required facts.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.joint_services import memory_write_runtime
from memory.maintenance.domain import assemble_candidates, canonical_request, validate_manifest
from memory.maintenance.snapshots import analysis_payload
from memory.revisions.domain import RevisionError, content_hash
from memory.structured_sources import canonical_source, evidence_snapshot, source_envelope, submission_metadata

FIXTURE = Path(__file__).parents[1] / "fixtures/memory_source_comparison_holdout.json"
SEALED_SHA256 = "102b70e308c0d0cae959d40777f17a9dd505dd3f3ba5813a4db9e499bb866175"


@pytest.fixture
def cases() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text())["cases"]


def test_holdout_is_the_preobservation_sealed_fixture() -> None:
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == SEALED_SHA256
    value = json.loads(FIXTURE.read_text())
    assert value["kind"] == "independent_holdout" and value["schema_version"] == 1 and len(value["cases"]) == 4
    assert len({case["id"] for case in value["cases"]}) == 4
    for case in value["cases"]:
        for source in case["sources"]:
            if source["event_time"] is not None:
                assert source["event_time"] in source["user_input"]
        for kind in ("required", "forbidden"):
            assert all(item["id"] and item["description"] for item in case["rubric"][kind])


class CaptureRaw:
    async def append_raw(self, memory_id: str, **options: Any) -> dict[str, Any]:
        assert memory_id == "bank"
        return options


async def paired_manifests(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, Any]]:
    async def factory(*args: Any, **kwargs: Any) -> CaptureRaw:
        return CaptureRaw()

    monkeypatch.setattr(memory_write_runtime, "service_for", factory)
    monkeypatch.setattr(memory_write_runtime, "current_timestamp", lambda: 1794837600000)
    manifests: tuple[dict[str, Any], dict[str, Any]] = ({"inputs": [], "entries": [], "estimated_input_tokens": 0}, {"inputs": [], "entries": [], "estimated_input_tokens": 0})
    async with AsyncSession() as db:
        for index, source in enumerate(case["sources"]):
            scope = {"user_id": "subject", "agent_id": "agent", "session_id": source["session_id"]}
            # Capture the actual old producer formatter rather than reimplement it.
            legacy = await memory_write_runtime.append_message(
                db, "bank", {**scope, "user_input": source["user_input"], "agent_response": source["agent_response"]}, operation_id="legacy-" + str(index), actor_id="owner"
            )
            envelope = source_envelope([{"role": "user", "content": source["user_input"]}, {"role": "assistant", "content": source["agent_response"]}], source["event_time"])
            structured = canonical_source(envelope)
            receipt = submission_metadata(
                actor_id="owner", operation_id="structured-" + str(index), content_sha256=content_hash(structured), received_at="2026-11-16T14:00:00Z", reference_time=legacy["valid_at"]
            )
            assert envelope["segments"][0]["content"] == source["user_input"]
            assert envelope["segments"][1]["content"] == source["agent_response"]
            for branch, (content, metadata) in enumerate(((legacy["content"], None), (structured, receipt))):
                identity = 10 + index + 100 * branch
                manifests[branch]["inputs"].append(
                    {
                        "input_id": "source-" + str(identity),
                        "source_id": identity,
                        "source_revision": 1,
                        "source_generation": 1,
                        "content_sha256": content_hash(content),
                        "scope": deepcopy(scope),
                        "valid_at": legacy["valid_at"],
                        "snapshot": evidence_snapshot(content, metadata),
                        "coverage": "included",
                        "defer_reason": None,
                    }
                )
    for manifest in manifests:
        manifest["estimated_input_tokens"] = len(json.dumps(analysis_payload(manifest, case["policy"]), ensure_ascii=False))
        request = canonical_request([item["source_id"] for item in manifest["inputs"]], [], case["policy"], 32768, 8192)
        validate_manifest(manifest, request, len)
    return manifests


@pytest.mark.parametrize("index", range(4))
async def test_paired_production_manifests_preserve_all_inputs_without_promoting_declared_roles(index: int, cases: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    case = cases[index]
    legacy, structured = await paired_manifests(case, monkeypatch)
    assert len(legacy["inputs"]) == len(structured["inputs"]) == len(case["sources"])
    for old, new in zip(legacy["inputs"], structured["inputs"], strict=True):
        assert old["scope"] == new["scope"] and old["valid_at"] == new["valid_at"]
        assert old["snapshot"]["origin"] == "legacy_combined" and old["snapshot"]["role_status"] == "role_unknown"
        assert new["snapshot"]["role_status"] == "caller_declared"
        assert new["snapshot"]["source_metadata"]["provenance"]["speaker_identity_verified"] is False
        assert new["snapshot"]["source_metadata"]["provenance"]["content_verified"] is False
    for manifest in (legacy, structured):
        payload = analysis_payload(manifest, case["policy"])
        assert len(payload["inputs"]) == len(case["sources"])
        assert "rubric" not in payload and "expected_source_indices" not in payload
        # An empty, valid proposal list is a mechanism result, not a rubric pass.
        assert assemble_candidates([], manifest, len, 8192) == []


@pytest.mark.parametrize("branch", [0, 1])
async def test_cross_session_patch_requires_its_own_complete_source_closure(branch: int, cases: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    case = next(item for item in cases if len(item["sources"]) == 2)
    manifest = (await paired_manifests(case, monkeypatch))[branch]
    old, new = manifest["inputs"]
    target = {
        "entry_id": "a" * 32,
        "revision": 1,
        "generation": 1,
        "message_id": 500,
        "content": "待改片段；并行事实甲；并行事实乙。",
        "content_sha256": content_hash("待改片段；并行事实甲；并行事实乙。"),
        "scope": deepcopy(old["scope"]),
        "message_type": "semantic",
        "source_ids": [old["source_id"]],
    }
    manifest["entries"] = [target]
    patch = {
        "action": "patch",
        "target_entry_id": target["entry_id"],
        "patch": {"old_text": "待改片段", "new_text": "新片段"},
        "source_input_ids": [new["input_id"]],
        "reason": "Controlled host-contract patch, not model quality",
    }
    with pytest.raises(RevisionError):
        assemble_candidates([patch, {"action": "noop", "source_input_ids": [old["input_id"]], "reason": "The other proposal cannot donate its references"}], manifest, len, 8192)
    patch["source_input_ids"] = [old["input_id"], new["input_id"]]
    candidate = assemble_candidates([patch], manifest, len, 8192)[0]
    assert candidate["content"] == "新片段；并行事实甲；并行事实乙。"
    assert {source["source_id"] for source in candidate["sources"]} == {old["source_id"], new["source_id"]}
    assert candidate["assessment"] == {"kind": "hypothesis", "verification_status": "unverified"}


@pytest.mark.parametrize("branch", [0, 1])
async def test_host_rejects_cross_principal_model_merge_in_either_format(branch: int, cases: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    case = next(item for item in cases if len(item["sources"]) == 2)
    manifest = (await paired_manifests(case, monkeypatch))[branch]
    manifest["inputs"][1]["scope"]["user_id"] = "other-subject"
    proposal = {
        "action": "add",
        "message_type": "semantic",
        "content": "Controlled model output",
        "source_input_ids": [item["input_id"] for item in manifest["inputs"]],
        "reason": "Attempted cross-principal merge",
    }
    with pytest.raises(RevisionError, match="principal_mismatch"):
        assemble_candidates([proposal], manifest, len, 8192)
