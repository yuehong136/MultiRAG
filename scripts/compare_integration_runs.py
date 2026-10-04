"""Compare successful, identical case sets; refuse partial or incomparable runs."""

import argparse
import json
from pathlib import Path
from typing import Any


def load_run(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads((directory / "run.json").read_text())
    if manifest["exit_code"] != 0:
        raise ValueError(f"Incomplete or failed run: {directory}")
    cases: dict[str, Any] = {}
    selected: set[str] = set()
    for path in sorted(directory.glob("execution-*.json")):
        report = json.loads(path.read_text())
        if report["exit_code"] != 0:
            raise ValueError(f"Failed worker: {path.name}")
        if any(item["outcome"] != "passed" for item in report["collection"]):
            raise ValueError(f"Incomplete collection: {path.name}")
        selected.update(report["selected"])
        for name, phases in report["tests"].items():
            if name in cases or set(phases) != {"setup", "call", "teardown"} or any(phase["outcome"] != "passed" for phase in phases.values()):
                raise ValueError(f"Duplicate, skipped or incomplete case: {name}")
            cases[name] = phases
    if not cases:
        raise ValueError("No completed cases to compare")
    if selected != set(cases):
        raise ValueError("Selected cases are missing execution evidence")
    return manifest, cases


def compare(before: Path, after: Path) -> dict[str, Any]:
    left, old = load_run(before)
    right, new = load_run(after)
    if set(old) != set(new):
        raise ValueError("Case sets differ; reduced coverage is not a speed improvement")
    if not left.get("environment") or left["environment"] != right.get("environment"):
        raise ValueError("Python/platform/package fingerprints differ or are unavailable")
    versions = lambda run: {name: value.get("version") for name, value in run["services"].items()}
    if versions(left) != versions(right):
        raise ValueError("Service versions differ")
    old_seconds, new_seconds = left["total_seconds"], right["total_seconds"]
    phases = {
        phase: {"before": round(sum(case[phase]["seconds"] for case in old.values()), 3), "after": round(sum(case[phase]["seconds"] for case in new.values()), 3)}
        for phase in ("setup", "call", "teardown")
    }
    return {
        "cases": len(old),
        "before_seconds": old_seconds,
        "after_seconds": new_seconds,
        "wall_time_reduction_percent": round((1 - new_seconds / old_seconds) * 100, 2),
        "summed_case_phase_seconds": phases,
        "service_versions": versions(left),
        "environment": left["environment"],
        "note": "Phase sums overlap under parallel execution. One paired observation, not a statistical guarantee.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    options = parser.parse_args()
    try:
        result = compare(options.before, options.after)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
