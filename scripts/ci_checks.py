"""CI scope, complete core partitions, and the final required check."""

import argparse
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CORE_SHARDS = 4
RUNTIME_JOBS = frozenset({"typecheck", "unit", "integration", "integration-infinity", "consumer", "go", "smoke"})
# These tests require separate owned HTTP/SQL harnesses; ordinary skips are errors.
GO_LIVE_OPT_INS = {
    "admin": {"TestAdminVariablesCLIHTTPAndSQL"},
    "router": {"TestRESTAliasesHTTPAndSQL", "TestMemoryUpdateHTTPAndSQL", "TestDocumentListHTTPCLIAndSQL"},
    "dao": {"TestBaseModelTimeScratchPostgres", "TestTenantModelExtraScratchPostgres"},
    "engine": {"TestSkillMilvusLiveUnpublishedFragments", "TestEngineStoresLive"},
    "handler": {
        "TestDatasetChunkMetadataCLIAndStores",
        "TestAfterD7ProvidersHTTPCLIAndSQL",
        "TestDatasetGraphHTTPAndSQL",
        "TestAfter9dProvidersHTTPCLIAndSQL",
        "TestAfter410GoHTTPCLIAndSQL",
        "TestAfter440ProvidersHTTPCLIAndSQL",
        "TestAfterBBBProvidersHTTPCLIAndSQL",
        "TestChatSessionLiveHTTPAndSQL",
        "TestDatasetReadHTTPAndSQL",
        "TestDatasetTimeHTTPAndSQL",
        "TestDocumentStatusLiveServer",
        "TestHybridAuthLiveServer",
        "TestHostedProviderRoutesScratchPostgres",
        "TestOllamaInstanceLiveHTTPAndSQL",
        "TestProviderInstancesLiveHTTPAndSQL",
        "TestGoogleProviderLiveHTTPAndSQL",
        "TestGoogleEmbeddingLiveHTTPAndSQL",
        "TestMediaHTTPCLIAndSQL",
        "TestModelClassProviderLiveHTTPAndSQL",
        "TestModelInferenceLiveHTTPAndSQL",
        "TestNVIDIASiliconFlowEmbeddingLiveCLIHTTPAndSQL",
        "TestSkillCoreLiveServer",
        "TestSpeechProvidersHTTPCLIAndSQL",
        "TestTaskLiveServer",
        "TestUserTimeHTTPCLIAndSQL",
        "TestXAIInstanceLiveHTTPAndSQL",
    },
    "service": {"TestAfter2836ModelsHTTPCLIAndSQL", "TestChatSessionScratchPostgres", "TestHostedProviderBindingScratchPostgres", "TestModelBindingScratchPostgres"},
    "service/nlp": {"TestRetrievalScratchPostgresInfinity"},
    "skills": {"TestSkillsLiveServer"},
}


def requires_runtime(paths: list[str]) -> bool:
    """Unknown/empty selections fail closed; test fixtures are not documentation."""

    def documentation(path: str) -> bool:
        return path in {"AGENTS.md", "CLAUDE.md", "README.md", "tests/README.md", ".github/copilot-instructions.md"} or (path.startswith("docs/") and path.endswith((".md", ".rst")))

    return not paths or any(not documentation(path) for path in paths)


def changed_scope(event: str, payload: dict[str, Any]) -> bool:
    if event == "pull_request":
        base = payload.get("pull_request", {}).get("base", {}).get("sha", "")
    elif event == "merge_group":
        base = payload.get("merge_group", {}).get("base_sha", "")
    elif event == "push":
        base = payload.get("before", "")
    else:
        return True  # Explicit dispatches always revalidate.
    if not re.fullmatch(r"[0-9a-f]{40}", base) or base == "0" * 40:
        return True
    result = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", base, "HEAD"], cwd=ROOT, capture_output=True, check=False)
    if result.returncode:
        return True
    return requires_runtime([path for path in result.stdout.decode().split("\0") if path])


def core_partition(index: int, count: int = CORE_SHARDS) -> list[Path]:
    from tests.support.integration_suites import suite_paths

    if count != CORE_SHARDS or not 0 <= index < count:
        raise ValueError("Core CI requires four disjoint shards indexed 0..3")
    return suite_paths("core")[index::count]


def audit_core(directory: Path, sha: str) -> None:
    """Every core file must occur exactly once in successful same-SHA reports."""
    from tests.support.integration_suites import suite_paths

    seen: Counter[str] = Counter()
    for index in range(CORE_SHARDS):
        root = directory / f"core-{index}"
        identity = json.loads((root / "partition.json").read_text())
        run = json.loads((root / "run.json").read_text())
        expected = [str(path.relative_to(ROOT)) for path in core_partition(index)]
        if identity != {"sha": sha, "index": index, "count": CORE_SHARDS, "paths": expected} or run.get("paths") != expected or run.get("suite") != "core" or run.get("exit_code") != 0:
            raise ValueError(f"Incomplete or mismatched core shard {index}")
        document = ET.parse(root / "junit.xml").getroot()
        suites = document.iter("testsuite")
        totals = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
        for suite in suites:
            for key in totals:
                totals[key] += int(suite.attrib.get(key, 0))
        xfails = sum(element.attrib.get("type") == "pytest.xfail" for element in document.iter("skipped"))
        if totals["tests"] <= xfails or totals["failures"] or totals["errors"] or totals["skipped"] != xfails:
            raise ValueError(f"Unsuccessful core shard {index}")
        seen.update(expected)
    if seen != Counter(str(path.relative_to(ROOT)) for path in suite_paths("core")):
        raise ValueError("Core partition omitted or duplicated files")


def required_gate(needs: dict[str, Any]) -> None:
    expected = {"changes", "lint", "security"} | RUNTIME_JOBS
    if set(needs) != expected or needs["changes"]["result"] != "success":
        raise ValueError("Required job inventory or scope result is incomplete")
    runtime = needs["changes"].get("outputs", {}).get("runtime")
    if runtime not in {"true", "false"}:
        raise ValueError("Missing runtime scope decision")
    for name, job in needs.items():
        wanted = "skipped" if runtime == "false" and name in RUNTIME_JOBS else "success"
        if job["result"] != wanted:
            raise ValueError(f"Required job {name}: {job['result']} (expected {wanted})")


def audit_go(path: Path) -> dict[str, Any]:
    allowed = {f"multirag/internal/{package}::{test}" for package, tests in GO_LIVE_OPT_INS.items() for test in tests}
    started: Counter[str] = Counter()
    finished: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    opt_ins: list[str] = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        action, package, test = event["Action"], event.get("Package", ""), event.get("Test")
        if action in {"fail", "build-fail"}:
            raise ValueError(f"Failed Go event: {package}::{test}")
        if action == "start":
            started[package] += 1
        if action in {"pass", "skip"}:
            counts[f"{action}_{'test' if test else 'package'}"] += 1
            if not test:
                finished[package] += 1
            elif action == "skip":
                identity = f"{package}::{test}"
                if identity not in allowed:
                    raise ValueError(f"Unexpected Go skip: {identity}")
                opt_ins.append(identity)
    if not started or started != finished or not counts["pass_test"]:
        raise ValueError("Empty or incomplete Go evidence")
    return {"counts": dict(counts), "unverified_live_opt_ins": opt_ins}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scope", "core", "audit", "go-audit", "required"))
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--reports", type=Path, default=ROOT / ".test-results")
    args = parser.parse_args()
    if args.command == "scope":
        runtime = changed_scope(os.environ["GITHUB_EVENT_NAME"], json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()))
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(f"runtime={str(runtime).lower()}\n")
    elif args.command == "core":
        from scripts.run_integration import main as run

        paths = [str(path.relative_to(ROOT)) for path in core_partition(args.index)]
        report = args.reports / f"core-{args.index}"
        report.mkdir(parents=True, exist_ok=True)
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        (report / "partition.json").write_text(json.dumps({"sha": sha, "index": args.index, "count": CORE_SHARDS, "paths": paths}, indent=2))
        return run(["--suite", "core", "--workers", "0", "--report-dir", str(report), "--", *paths, "-q"])
    elif args.command == "audit":
        audit_core(args.reports, os.environ["GITHUB_SHA"])
    elif args.command == "go-audit":
        summary = audit_go(args.reports / "tests.jsonl")
        (args.reports / "summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2))
    else:
        required_gate(json.loads(os.environ["NEEDS_JSON"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
