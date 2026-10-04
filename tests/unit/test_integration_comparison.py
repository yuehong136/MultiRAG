"""A faster partial or failed run must never count as a performance gain."""

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.compare_integration_runs import compare


def write_run(directory: Path, seconds: int = 10) -> tuple[dict[str, Any], dict[str, Any]]:
    directory.mkdir()
    manifest = {"exit_code": 0, "total_seconds": seconds, "environment": {"python": "3.12"}, "services": {"postgresql": {"version": "16"}}}
    report = {
        "exit_code": 0,
        "collection": [{"nodeid": "test_sample.py", "outcome": "passed"}],
        "selected": ["test_sample.py::test_one"],
        "tests": {"test_sample.py::test_one": {phase: {"outcome": "passed", "seconds": 1} for phase in ("setup", "call", "teardown")}},
    }
    (directory / "run.json").write_text(json.dumps(manifest))
    (directory / "execution-main.json").write_text(json.dumps(report))
    return manifest, report


def test_comparison_requires_matching_successful_coverage(tmp_path: Path) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    write_run(before)
    write_run(after, 6)
    result = compare(before, after)
    assert result["cases"] == 1
    assert result["wall_time_reduction_percent"] == 40


@pytest.mark.parametrize("defect", ["failed_run", "failed_worker", "missing_case", "skipped_case", "collection_error", "coverage", "environment", "service_version", "duplicate"])
def test_comparison_rejects_invalid_evidence(tmp_path: Path, defect: str) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    write_run(before)
    manifest, report = write_run(after, 6)
    if defect == "failed_run":
        manifest["exit_code"] = 1
    elif defect == "failed_worker":
        report["exit_code"] = 1
    elif defect == "missing_case":
        report["selected"].append("test_sample.py::test_missing")
    elif defect == "skipped_case":
        report["tests"][report["selected"][0]]["call"]["outcome"] = "skipped"
    elif defect == "collection_error":
        report["collection"][0]["outcome"] = "failed"
    elif defect == "coverage":
        report["selected"] = ["test_sample.py::test_other"]
        report["tests"] = {report["selected"][0]: next(iter(report["tests"].values()))}
    elif defect == "environment":
        manifest["environment"]["python"] = "3.13"
    elif defect == "service_version":
        manifest["services"]["postgresql"]["version"] = "17"
    else:
        (after / "execution-gw0.json").write_text(json.dumps(report))
    (after / "run.json").write_text(json.dumps(manifest))
    (after / "execution-main.json").write_text(json.dumps(report))
    with pytest.raises(ValueError):
        compare(before, after)
