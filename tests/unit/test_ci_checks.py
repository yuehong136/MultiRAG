"""CI must retain complete coverage and reject partial/cancelled evidence."""

import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_checks import CORE_SHARDS, ROOT, RUNTIME_JOBS, audit_core, audit_go, changed_scope, core_partition, required_gate, requires_runtime
from tests.support.integration_suites import PARALLEL_TESTS, required_services, suite_paths


@pytest.mark.parametrize("paths", [["AGENTS.md"], ["docs/development.md", "tests/README.md"]])
def test_documentation_only_needs_no_runtime(paths: list[str]) -> None:
    assert not requires_runtime(paths)


@pytest.mark.parametrize("path", ["api/apps/llm_app.py", ".github/workflows/ci.yml", ".github/consumer-web.json", "tests/evals/corpus/v1/example.md", "uv.lock", "go.mod"])
def test_executable_contracts_and_fixtures_keep_full_ci(path: str) -> None:
    assert requires_runtime(["AGENTS.md", path])
    assert requires_runtime([])


def test_new_core_files_are_never_omitted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = [*suite_paths("core"), tmp_path / "test_new_contract.py"]
    monkeypatch.setattr("tests.support.integration_suites.suite_paths", lambda suite: paths)
    assert Counter(path for i in range(CORE_SHARDS) for path in core_partition(i)) == Counter(paths)
    with pytest.raises(ValueError):
        core_partition(CORE_SHARDS)


@pytest.mark.parametrize("name", ["test_agent_file_authorization.py", "test_legacy_document_counters.py"])
def test_agent_files_and_worker_counters_keep_serial_storage_contract(name: str) -> None:
    path = ROOT / "tests/integration" / name
    assert path.is_file()
    assert sum(path in core_partition(index) for index in range(CORE_SHARDS)) == 1
    assert path not in suite_paths("db")
    assert name not in PARALLEL_TESTS
    assert required_services([path]) == {"postgresql", "redis", "minio", "milvus"}


@pytest.mark.parametrize("event", ["pull_request", "merge_group", "push"])
def test_scope_uses_final_checkout_and_fails_closed(monkeypatch: pytest.MonkeyPatch, event: str) -> None:
    base = "a" * 40
    payload = {"pull_request": {"base": {"sha": base}}, "merge_group": {"base_sha": base}, "before": base}

    def diff(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert command == ["git", "diff", "--name-only", "--no-renames", "-z", base, "HEAD"]
        return subprocess.CompletedProcess(command, 0, b"AGENTS.md\0docs/development.md\0")

    monkeypatch.setattr("scripts.ci_checks.subprocess.run", diff)
    assert not changed_scope(event, payload)
    monkeypatch.setattr("scripts.ci_checks.subprocess.run", lambda *args, **kwargs: subprocess.CompletedProcess([], 128, b""))
    assert changed_scope(event, payload)
    assert changed_scope(event, {})
    assert changed_scope("push", {"before": "0" * 40})
    assert changed_scope("workflow_dispatch", payload)


def needs(runtime: bool) -> dict[str, Any]:
    return {name: {"result": "skipped" if not runtime and name in RUNTIME_JOBS else "success", "outputs": {"runtime": str(runtime).lower()}} for name in {"changes", "lint", "security"} | RUNTIME_JOBS}


@pytest.mark.parametrize("runtime", [True, False])
def test_required_gate_accepts_only_the_expected_jobs(runtime: bool) -> None:
    required_gate(needs(runtime))
    for name in needs(runtime):
        for result in ("failure", "cancelled", "skipped" if runtime else "unknown"):
            damaged = needs(runtime)
            damaged[name]["result"] = result
            with pytest.raises(ValueError):
                required_gate(damaged)
    incomplete = needs(runtime)
    del incomplete["consumer"]
    with pytest.raises(ValueError):
        required_gate(incomplete)
    incomplete = needs(runtime)
    incomplete["changes"]["outputs"] = {}
    with pytest.raises(ValueError):
        required_gate(incomplete)


def test_core_audit_rejects_stale_partial_and_skipped_reports(tmp_path: Path) -> None:
    sha = "a" * 40
    for i in range(CORE_SHARDS):
        root = tmp_path / f"core-{i}"
        root.mkdir()
        paths = [str(p.relative_to(ROOT)) for p in core_partition(i)]
        (root / "partition.json").write_text(json.dumps({"sha": sha, "index": i, "count": CORE_SHARDS, "paths": paths}))
        (root / "run.json").write_text(json.dumps({"suite": "core", "exit_code": 0, "paths": paths}))
        (root / "junit.xml").write_text('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"/></testsuites>')
    audit_core(tmp_path, sha)
    with pytest.raises(ValueError):
        audit_core(tmp_path, "b" * 40)
    run = tmp_path / "core-0/run.json"
    saved = run.read_text()
    run.write_text(json.dumps({"suite": "core", "exit_code": 0, "paths": []}))
    with pytest.raises(ValueError):
        audit_core(tmp_path, sha)
    run.write_text(saved)
    (tmp_path / "core-0/junit.xml").write_text('<testsuites><testsuite tests="0"/></testsuites>')
    with pytest.raises(ValueError):
        audit_core(tmp_path, sha)
    (tmp_path / "core-0/junit.xml").unlink()
    with pytest.raises(FileNotFoundError):
        audit_core(tmp_path, sha)
    (tmp_path / "core-0/junit.xml").write_text('<testsuites><testsuite tests="1" skipped="1"/></testsuites>')
    with pytest.raises(ValueError):
        audit_core(tmp_path, sha)
    (tmp_path / "core-0/junit.xml").write_text('<testsuites><testsuite tests="2" skipped="1"><testcase><skipped type="pytest.xfail"/></testcase></testsuite></testsuites>')
    audit_core(tmp_path, sha)


def test_go_reports_reject_unexpected_skips_failures_and_truncation(tmp_path: Path) -> None:
    path = tmp_path / "go.jsonl"
    package = "multirag/internal/handler"
    events: list[dict[str, str]] = [
        {"Action": "start", "Package": package},
        {"Action": "pass", "Package": package, "Test": "TestUnit"},
        {"Action": "skip", "Package": package, "Test": "TestChatSessionLiveHTTPAndSQL"},
        {"Action": "skip", "Package": package, "Test": "TestModelInferenceLiveHTTPAndSQL"},
        {"Action": "skip", "Package": package, "Test": "TestDatasetTimeHTTPAndSQL"},
        {"Action": "skip", "Package": package, "Test": "TestHostedProviderRoutesScratchPostgres"},
        {"Action": "start", "Package": "multirag/internal/service"},
        {"Action": "skip", "Package": "multirag/internal/service", "Test": "TestHostedProviderBindingScratchPostgres"},
        {"Action": "pass", "Package": "multirag/internal/service"},
        {"Action": "pass", "Package": package},
    ]
    path.write_text("\n".join(map(json.dumps, events)))
    result = audit_go(path)
    assert result["counts"]["pass_test"] == 1
    assert result["unverified_live_opt_ins"] == [
        f"{package}::TestChatSessionLiveHTTPAndSQL",
        f"{package}::TestModelInferenceLiveHTTPAndSQL",
        f"{package}::TestDatasetTimeHTTPAndSQL",
        f"{package}::TestHostedProviderRoutesScratchPostgres",
        "multirag/internal/service::TestHostedProviderBindingScratchPostgres",
    ]
    for bad in [
        [],
        events[:-1],
        [*events, {"Action": "fail", "Package": package}],
        [*events, {"Action": "skip", "Package": package, "Test": "TestOrdinary"}],
        [*events, {"Action": "skip", "Package": package, "Test": "TestModelInferenceLiveHTTPAndSQL/unknown"}],
        [*events, {"Action": "skip", "Package": package, "Test": "TestDatasetTimeHTTPAndSQL/unknown"}],
        [*events, {"Action": "skip", "Package": package, "Test": "TestHostedProviderRoutesScratchPostgres/unknown"}],
        [*events, {"Action": "skip", "Package": "multirag/internal/service", "Test": "TestHostedProviderBindingScratchPostgres/unknown"}],
    ]:
        path.write_text("\n".join(map(json.dumps, bad)))
        with pytest.raises(ValueError):
            audit_go(path)


@pytest.mark.parametrize(
    "test",
    ["TestDatasetReadHTTPAndSQL", "TestGoogleEmbeddingLiveHTTPAndSQL", "TestMediaHTTPCLIAndSQL", "TestNVIDIASiliconFlowEmbeddingLiveCLIHTTPAndSQL"],
)
def test_owned_handler_opt_ins_remain_unverified_when_skipped(tmp_path: Path, test: str) -> None:
    path = tmp_path / "go.jsonl"
    package = "multirag/internal/handler"
    events = [
        {"Action": "start", "Package": package},
        {"Action": "pass", "Package": package, "Test": "TestOrdinary"},
        {"Action": "skip", "Package": package, "Test": test},
        {"Action": "pass", "Package": package},
    ]
    path.write_text("\n".join(map(json.dumps, events)))
    assert audit_go(path) == {
        "counts": {"pass_test": 1, "skip_test": 1, "pass_package": 1},
        "unverified_live_opt_ins": [f"{package}::{test}"],
    }
    # A successful actual execution is reported as a pass, separately from the
    # ordinary no-service run where the same explicit test was not executed.
    events[2]["Action"] = "pass"
    path.write_text("\n".join(map(json.dumps, events)))
    assert audit_go(path) == {"counts": {"pass_test": 2, "pass_package": 1}, "unverified_live_opt_ins": []}


@pytest.mark.parametrize(
    ("package", "test"),
    [
        ("multirag/internal/handler", "TestDatasetReadHTTPAndSQL/unknown"),
        ("multirag/internal/handler", "TestGoogleEmbeddingLiveHTTPAndSQL/unknown"),
        ("multirag/internal/handler", "TestMediaHTTPCLIAndSQL/unknown"),
        ("multirag/internal/handler", "TestNVIDIASiliconFlowEmbeddingLiveCLIHTTPAndSQL/unknown"),
        ("multirag/internal/service", "TestDatasetReadHTTPAndSQL"),
        ("multirag/internal/service", "TestGoogleEmbeddingLiveHTTPAndSQL"),
        ("multirag/internal/service", "TestMediaHTTPCLIAndSQL"),
        ("multirag/internal/service", "TestNVIDIASiliconFlowEmbeddingLiveCLIHTTPAndSQL"),
    ],
)
def test_owned_opt_in_registration_does_not_allow_other_skips(tmp_path: Path, package: str, test: str) -> None:
    path = tmp_path / "go.jsonl"
    events = [
        {"Action": "start", "Package": package},
        {"Action": "pass", "Package": package, "Test": "TestOrdinary"},
        {"Action": "skip", "Package": package, "Test": test},
        {"Action": "pass", "Package": package},
    ]
    path.write_text("\n".join(map(json.dumps, events)))
    with pytest.raises(ValueError, match="Unexpected Go skip"):
        audit_go(path)


@pytest.mark.parametrize("failure", ["failed_opt_in", "failed_package", "build_failed_package", "unfinished_package", "unfinished_other_package"])
def test_allowed_opt_ins_do_not_hide_failed_or_unfinished_packages(tmp_path: Path, failure: str) -> None:
    path = tmp_path / "go.jsonl"
    package = "multirag/internal/handler"
    events = [
        {"Action": "start", "Package": package},
        {"Action": "pass", "Package": package, "Test": "TestOrdinary"},
        {"Action": "skip", "Package": package, "Test": "TestMediaHTTPCLIAndSQL"},
        {"Action": "pass", "Package": package},
    ]
    if failure == "failed_opt_in":
        events[2]["Action"] = "fail"
    elif failure == "failed_package":
        events[-1]["Action"] = "fail"
    elif failure == "build_failed_package":
        events.append({"Action": "build-fail", "Package": "multirag/internal/service"})
    elif failure == "unfinished_package":
        events.pop()
    else:
        events.append({"Action": "start", "Package": "multirag/internal/service"})
    path.write_text("\n".join(map(json.dumps, events)))
    with pytest.raises(ValueError, match=r"Failed Go event|Empty or incomplete Go evidence"):
        audit_go(path)


@pytest.mark.parametrize(
    "identity",
    [
        "multirag/internal/skills::TestLegacyEmbeddingScratchPostgres",
        "multirag/internal/router::TestAfter33GManagementHTTPAndSQL",
        "multirag/internal/router::TestAfter33GManagementHTTPAndSQL/built_CLI_consumer",
    ],
)
def test_ci_go_requires_service_tests(tmp_path: Path, identity: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.ci_checks import GO_LIVE_OPT_INS, GO_REQUIRED_TESTS

    path = tmp_path / "go.jsonl"
    events = []
    for required in sorted(GO_REQUIRED_TESTS):
        package, test = required.split("::")
        events.extend([{"Action": "start", "Package": package}, {"Action": "pass", "Package": package, "Test": test}, {"Action": "pass", "Package": package}])
    path.write_text("\n".join(map(json.dumps, events)))
    audit_go(path, require_ci_tests=True)
    package, test = identity.split("::")
    omitted = [e for e in events if not (e.get("Test") == test and e["Package"] == package)]
    path.write_text("\n".join(map(json.dumps, omitted)))
    with pytest.raises(ValueError, match="Missing required"):
        audit_go(path, require_ci_tests=True)
    monkeypatch.setitem(GO_LIVE_OPT_INS, package.removeprefix("multirag/internal/"), {test})
    for event in events:
        if event.get("Test") == test:
            event["Action"] = "skip"
    path.write_text("\n".join(map(json.dumps, events)))
    with pytest.raises(ValueError, match="Unexpected Go skip"):
        audit_go(path, require_ci_tests=True)
