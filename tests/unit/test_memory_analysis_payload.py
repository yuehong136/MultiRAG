"""Deterministic target citations are model input, never repaired model output."""

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from memory.maintenance import provider
from memory.maintenance.domain import assemble_candidates
from memory.maintenance.snapshots import ANALYSIS_INPUT_CONTRACT, MaintenanceSnapshots, analysis_payload
from memory.revisions.domain import RevisionValidationError, content_hash
from memory.revisions.storage import ManagedMessageStore
from memory.writes.providers import MemoryWriteProviders


def manifest() -> dict[str, Any]:
    scope = {"user_id": "subject", "agent_id": "agent", "session_id": "first"}
    content = "旧项目说明；保留必要条件。"
    inputs = [
        {
            "input_id": alias,
            "source_id": identity,
            "source_revision": 1,
            "source_generation": 1,
            "content_sha256": content_hash(text),
            "scope": {**scope, "session_id": session},
            "snapshot": {"content": text, "role_status": "role_unknown"},
            "coverage": "included",
            "defer_reason": None,
        }
        for identity, alias, text, session in ((10, "old-input-alias", content, "first"), (20, "new-input-alias", "补录的限定条件。", "second"))
    ]
    return {
        "inputs": inputs,
        "entries": [{"entry_id": "entry", "revision": 1, "source_ids": [10], "content": content, "content_sha256": content_hash(content), "scope": scope}],
        "estimated_input_tokens": 0,
    }


@pytest.mark.parametrize("old_alias", ["old-input-alias", "part-0123456789abcdef01234567"])
def test_required_citations_use_current_input_alias_and_payload_does_not_mutate_manifest(old_alias: str) -> None:
    original = manifest()
    original["inputs"][0]["input_id"] = old_alias
    before = deepcopy(original)
    packed = analysis_payload(original, "decisions")
    assert packed["entries"][0]["required_source_input_ids"] == [old_alias]
    assert original == before and "required_source_input_ids" not in original["entries"][0]
    packed["entries"][0]["source_ids"].append(20)
    packed["entries"][0]["scope"]["user_id"] = "changed"
    packed["inputs"][0]["snapshot"]["content"] = "changed"
    assert original == before
    assert ANALYSIS_INPUT_CONTRACT != "whole_source_json"


def test_deferred_alias_is_counted_without_revealing_deferred_body_or_authorizing_a_patch() -> None:
    original = manifest()
    original["inputs"][0].update(coverage="deferred", defer_reason="input_budget", snapshot={})
    packed = analysis_payload(original, "decisions")
    assert packed["entries"][0]["required_source_input_ids"] == ["old-input-alias"]
    assert [item["input_id"] for item in packed["inputs"]] == ["new-input-alias"]
    proposal = {
        "action": "patch",
        "target_entry_id": "entry",
        "patch": {"old_text": "旧项目说明", "new_text": "补录说明"},
        "source_input_ids": ["old-input-alias", "new-input-alias"],
        "reason": "引用未实际送入的旧依据仍无效。",
    }
    with pytest.raises(RevisionValidationError, match="maintenance_evidence_out_of_scope"):
        assemble_candidates([proposal], original, len, 4096)


def test_each_target_carries_its_own_complete_old_source_alias_set() -> None:
    original = manifest()
    original["entries"].append({**deepcopy(original["entries"][0]), "entry_id": "another-entry", "source_ids": [20, 10]})
    targets = analysis_payload(original, "decisions")["entries"]
    assert targets[0]["required_source_input_ids"] == ["old-input-alias"]
    assert targets[1]["required_source_input_ids"] == ["new-input-alias", "old-input-alias"]
    assert "required_source_input_ids" not in original["entries"][1]


@pytest.mark.parametrize("malformed", ["missing", "ambiguous"])
async def test_missing_or_ambiguous_target_mapping_fails_before_bundle_construction(monkeypatch: pytest.MonkeyPatch, malformed: str) -> None:
    original = manifest()
    if malformed == "missing":
        original["inputs"].pop(0)
    else:
        original["inputs"].append({**deepcopy(original["inputs"][0]), "input_id": "different-alias-for-old-source"})
    before = deepcopy(original)
    constructed: list[object] = []

    def construct(prepared: Any) -> object:
        constructed.append(prepared)
        raise AssertionError("invalid mapping must be rejected before model construction")

    async with AsyncSession() as db:

        class Configuration:
            def __init__(self) -> None:
                self.db = db

            async def _prepared(self, memory_id: str, capability: str) -> tuple[Any, Any]:
                assert (memory_id, capability) == ("bank", "chat")
                return {}, SimpleNamespace(model_config={"llm_name": "controlled", "llm_factory": "unit"})

            @staticmethod
            def _chat_fingerprint(settings: Any, prepared: Any) -> str:
                return "controlled-config"

        monkeypatch.setattr(provider.LLMBundle, "from_prepared", staticmethod(construct))
        error = "maintenance_target_input_mapping_incomplete" if malformed == "missing" else "maintenance_target_input_mapping_ambiguous"
        with pytest.raises(RevisionValidationError, match=error):
            await provider.MaintenanceAnalyst(cast(MemoryWriteProviders, Configuration())).analyze("bank", original, "decisions", 512)
    assert constructed == [] and original == before


async def test_snapshot_budget_counts_the_complete_serialized_target_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    original = manifest()
    before = deepcopy(original)
    snapshots = MaintenanceSnapshots(cast(AsyncSession, object()), tenant_id="tenant", storage=cast(ManagedMessageStore, object()), count_tokens=len)

    async def evidence(memory_id: str, source_ids: list[int], entry_ids: list[str]) -> dict[str, Any]:
        assert (memory_id, source_ids, entry_ids) == ("bank", [10, 20], ["entry"])
        return deepcopy(original)

    monkeypatch.setattr(snapshots, "_evidence", evidence)
    # The code-point counter is deterministic test instrumentation, not a model tokenizer.
    old_metadata_body = {"policy": "preferences", "inputs": [], "entries": original["entries"]}
    old_metadata_budget = len(json.dumps(old_metadata_body, ensure_ascii=False, separators=(",", ":"))) + 512
    with pytest.raises(RevisionValidationError, match="dreaming_input_metadata_budget_exceeded"):
        await snapshots.snapshot("bank", [10, 20], ["entry"], old_metadata_budget)
    packed = await snapshots.snapshot("bank", [10, 20], ["entry"], 32768)
    body = json.dumps(analysis_payload(packed, "preferences"), ensure_ascii=False, separators=(",", ":"))
    assert packed["estimated_input_tokens"] == len(body) + 512
    assert '"required_source_input_ids":["old-input-alias"]' in body
    assert snapshots._estimate(packed) >= len(body) + 512
    assert original == before and "required_source_input_ids" not in packed["entries"][0]
