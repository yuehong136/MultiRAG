"""Replay synthetic baseline observations without invoking a paid provider.

Run through scripts/run_registered.py. --reference-oracle checks this harness,
not a real model. For real runs import run_baseline and inject the production
analyst, then save each EvaluationObservation as an observation JSON file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from memory.maintenance.evaluation import EvaluationObservation, load_cases, run_baseline


def write_report(path: Path, value: dict[str, Any]) -> None:
    # New report only; an earlier failed or partial observation is never erased.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).parents[1] / "fixtures/memory_maintenance_baseline.json")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--observations", type=Path, help="Case-ID keyed saved analysis/attempts/error_code observations")
    inputs.add_argument("--reference-oracle", action="store_true", help="Harness smoke with authored gold proposals; never model quality evidence")
    parser.add_argument("--prices", type=Path, help="Optional dated/source-linked per-model quote JSON; missing stays unknown")
    parser.add_argument("--out", type=Path, required=True, help="New private report path inside registered evidence")
    args = parser.parse_args()
    cases = load_cases(args.cases)
    observations = json.loads(args.observations.read_text()) if args.observations else {}
    prices = json.loads(args.prices.read_text()) if args.prices else None
    index = 0

    async def analyzer(manifest: dict[str, Any], policy: str, budget: int) -> EvaluationObservation:
        nonlocal index
        case = cases[index]
        index += 1
        if args.reference_oracle:
            return EvaluationObservation({"proposals": case["reference_proposals"]})
        saved = observations[case["case_id"]]
        return EvaluationObservation(saved.get("analysis"), saved.get("attempts", []), saved.get("error_code"))

    report = await run_baseline(cases, analyzer, count_tokens=len, token_counter="unicode_codepoints_budget_surrogate_not_provider_tokens", prices=prices)
    report["execution"] = "authored_reference_oracle_harness_smoke" if args.reference_oracle else "saved_model_observation_replay"
    report["observation_provenance"] = "authored test oracle" if args.reference_oracle else "caller-supplied replay; verify model/config/usage receipt independently"
    write_report(args.out, report)
    print(json.dumps({"case_count": report["case_count"], "execution": report["execution"], "outcome_counts": report["outcome_counts"], "answer_accuracy_measured": False}))


if __name__ == "__main__":
    asyncio.run(main())
