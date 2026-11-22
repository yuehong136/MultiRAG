"""Provision owned services for the controlled or explicitly authorized live comparison."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.manual.memory_restore_stores import isolated_stores
from tests.manual.memory_same_point_rollout import _child_environment
from tests.support.resource_journal import ResourceJournal

ROOT = Path(__file__).resolve().parents[2]


def private(path: Path, value: dict[str, Any], journal: ResourceJournal) -> None:
    journal.event("reserve-four-arm-private-input", path=str(path))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("controlled", "live"))
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--asset-root", required=True, type=Path)
    arguments = parser.parse_args()
    assert os.environ.get("MULTIRAG_RUN_ID") and os.environ.get("MULTIRAG_RUN_JOURNAL"), "Use scripts/run_registered.py"
    from common.config_utils import CONFIGS

    journal = ResourceJournal("four-arm-isolated")
    temporary = Path(os.environ["TMPDIR"]).resolve()
    work = temporary / "four-arm"
    journal.event("reserve-four-arm-directory", path=str(work))
    work.mkdir(mode=0o700)
    checkout_assets = ROOT / "core/res/deepdoc"
    added: list[tuple[Path, Path]] = []
    bindings = work / "provider-bindings.json"
    try:
        if arguments.mode == "live":
            from tests.support.memory_workload_models import configured_models

            identifiers = {kind: int(os.environ[f"MULTIRAG_MEMORY_{kind.upper()}_MODEL_ID"]) for kind in ("chat", "embedding")}
            configurations = configured_models(identifiers)
            assert configurations["chat"]["llm_name"] == "gpt-6.1-terra"
            assert configurations["embedding"]["llm_name"] == "text-embedding-3-small"
            assert all(value["llm_factory"] == "OpenAI" for value in configurations.values())
            private(bindings, configurations, journal)
            del configurations
        for name in ("det.onnx", "rec.onnx", "ocr.res", "updown_concat_xgb.model", "layout.onnx", "layout.laws.onnx", "layout.manual.onnx", "layout.paper.onnx", "tsr.onnx"):
            source = (arguments.asset_root / name).resolve(strict=True)
            target = checkout_assets / name
            assert not target.exists() and not target.is_symlink(), "Use an isolated checkout with no local runtime assets"
            journal.event("reserve-four-arm-borrowed-asset", link=str(target), target=str(source))
            target.symlink_to(source)
            added.append((target, source))
        with isolated_stores(work, journal, name="four-arm-eval") as stores:
            overlay = work / "isolated.json"
            private(overlay, stores.overlay(CONFIGS), journal)
            environment = _child_environment(overlay)
            environment.update(REQUIRE_SERVICES="1", INTEGRATION_NO_TESTCONTAINERS="1")
            if arguments.mode == "live":
                environment.update(
                    MULTIRAG_FOUR_ARM_PROVIDER_BINDINGS=str(bindings),
                    MULTIRAG_MEMORY_LIVE="1",
                    MULTIRAG_MEMORY_LIVE_PROVIDER="OpenAI",
                    MULTIRAG_EVAL_BASE_URL="https://api.openai.com/v1",
                )
            else:
                environment.pop("MULTIRAG_FOUR_ARM_HOLDOUT", None)
            selector = "test_live_four_arm" if arguments.mode == "live" else "test_controlled_four_arm"
            report = arguments.report_dir.resolve()
            journal.event("reserve-four-arm-report-directory", path=str(report), mode=arguments.mode)
            report.mkdir(mode=0o700)
            return subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "scripts/run_integration.py",
                    "--suite",
                    "eval-generation",
                    "--workers",
                    "0",
                    "--report-dir",
                    str(report),
                    "--",
                    "-q",
                    *(["-x"] if arguments.mode == "controlled" else []),
                    f"tests/evals/test_memory_four_arm.py::{selector}",
                ],
                cwd=ROOT,
                env=environment,
                check=False,
                timeout=2400,
            ).returncode
    finally:
        bindings.unlink(missing_ok=True)
        for target, source in added:
            assert target.is_symlink() and target.resolve() == source
            target.unlink()
        assert not bindings.exists() and all(not target.is_symlink() for target, _ in added)
        journal.event("four-arm-local-cleanup-readback", borrowed_asset_links_absent=True, provider_binding_file_absent=True)


if __name__ == "__main__":
    raise SystemExit(main())
