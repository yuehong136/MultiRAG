"""Account for every observed stage of the synthetic memory workload.

No provider, database, source text or credential is read here. Observations are
supplied by the real-provider harness; absent usage remains unknown.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from memory.maintenance.evaluation import usage_cost

STAGES = ("source_embedding", "analysis", "publication_embedding", "retrieval_embedding", "answer")
STAGE_STATES = frozenset({"completed", "blocked", "failed", "not_run", "unknown"})


def workload_cost(
    attempts: Sequence[Mapping[str, Any]],
    stage_states: Mapping[str, str],
    prices: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sum observed attempts without calling missing work free or verified."""
    if set(stage_states) != set(STAGES) or any(state not in STAGE_STATES for state in stage_states.values()):
        raise ValueError("invalid_workload_stage_states")
    copied = [deepcopy(dict(attempt)) for attempt in attempts]
    identities: set[str] = set()
    for attempt in copied:
        identity = attempt.get("attempt_id")
        if not isinstance(identity, str) or not identity or identity in identities:
            raise ValueError("invalid_workload_attempt_identity")
        identities.add(identity)
        if attempt.get("stage") not in STAGES:
            raise ValueError("invalid_workload_attempt_stage")
        if stage_states[attempt["stage"]] == "not_run":
            raise ValueError("unexecuted_stage_has_attempts")
    stages: dict[str, Any] = {}
    for stage in STAGES:
        rows = [attempt for attempt in copied if attempt["stage"] == stage]
        stages[stage] = {"state": stage_states[stage], **usage_cost(rows, prices)}
    total = usage_cost(copied, prices)
    all_stages_complete = all(state == "completed" for state in stage_states.values())
    return {
        "scope": list(STAGES),
        "stages": stages,
        "total": total,
        "observed_attempts": copied,
        "all_stages_complete": all_stages_complete,
        "whole_workload_tokens_known": all_stages_complete and total["provider_total_tokens"] is not None,
        "whole_workload_price_complete": all_stages_complete and total["price_complete"],
        "invoice_verified": False,
        "provider_internal_retries_observable": False,
        "excludes": ["idle_worker_host_cost", "storage_host_cost", "unobserved_provider_internal_retries"],
    }
