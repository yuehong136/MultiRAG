"""Acceptance must fail on false success, missing effects and lost readback."""

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.acceptance.api import MARKER, ProductAPI, response_data
from scripts.acceptance.evidence import AcceptanceError, Evidence
from scripts.run_integration import parallel_arguments, selected_paths
from tests.support.integration_suites import ACCEPTANCE, suite_paths


@pytest.mark.parametrize(
    "body", [{"code": 500, "data": {}}, {"retcode": 200, "data": {}}, {"code": 0, "retcode": 500, "data": {}}, {"code": False, "data": {}}, {"code": "0", "data": {}}, {"data": {}}, {"code": 0}, []]
)
def test_http_200_does_not_hide_business_failure(body: Any) -> None:
    with pytest.raises(AcceptanceError):
        response_data(body, label="test")


def test_http_failure_cannot_be_overridden_by_success_body() -> None:
    with pytest.raises(AcceptanceError, match="HTTP 401"):
        response_data({"code": 0, "data": {}}, label="test", http_status=401)


@pytest.mark.parametrize("status,progress,count", [("4", -1, 0), ("2", 0.3, 2), ("3", 1, 0), ("RUNNING", -1, 2)])
def test_failed_cancelled_or_empty_parse_cannot_pass(monkeypatch: pytest.MonkeyPatch, status: str, progress: float, count: int) -> None:
    api = ProductAPI("http://127.0.0.1", "unused", "scratch")
    monkeypatch.setattr(api, "document", lambda identifier: {"run": status, "progress": progress, "chunk_count": count})
    with pytest.raises(AcceptanceError):
        api.wait_parsed("document", timeout=0)
    api.session.close()


def test_progress_one_without_completed_status_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    api = ProductAPI("http://127.0.0.1", "unused", "scratch")
    monkeypatch.setattr(api, "document", lambda identifier: {"run": "1", "progress": 1, "chunk_count": 2})
    with pytest.raises(AcceptanceError, match="timed out"):
        api.wait_parsed("document", timeout=0)
    api.session.close()


@pytest.mark.parametrize(
    "data",
    [{"total": 0, "chunks": []}, {"total": 1, "chunks": [{"doc_id": "other", "content_with_weight": MARKER}]}, {"total": 1, "chunks": [{"doc_id": "owned", "content_with_weight": "wrong source"}]}],
)
def test_nonmatching_retrieval_cannot_pass(data: dict[str, Any]) -> None:
    with pytest.raises(AcceptanceError):
        ProductAPI.verify_retrieval(data, "owned")


def test_evidence_persists_failure_without_credentials(tmp_path: Path) -> None:
    report = Evidence(tmp_path, mode="full")
    report.secrets.append("private-test-token")

    def failure() -> None:
        raise RuntimeError("private-test-token")

    assert not report.check("upload", failure)
    assert not report.successful
    report.blocked("parse", "upload failed")
    report.finish()
    value = json.loads((tmp_path / "acceptance.json").read_text())
    assert value["automated_status"] == "incomplete_or_failed"
    assert value["visual_review"] == "pending_human_review"
    assert [check["status"] for check in value["checks"]] == ["failed", "blocked"]
    assert "private-test-token" not in (tmp_path / "acceptance.json").read_text()


def test_running_checks_cannot_report_completion(tmp_path: Path) -> None:
    report = Evidence(tmp_path, mode="full")
    assert report.check("upload", lambda: {"readback": True})
    assert json.loads((tmp_path / "acceptance.json").read_text())["automated_status"] == "running"
    report.finish()
    value = json.loads((tmp_path / "acceptance.json").read_text())
    assert value["automated_status"] == "passed"
    assert value["visual_review"] == "pending_human_review"


def test_product_suite_is_explicit_and_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    paths = suite_paths("acceptance")
    assert paths == [ACCEPTANCE / "test_product.py"]
    assert not set(paths).intersection(suite_paths("core"))
    assert selected_paths("acceptance", [str(paths[0])]) == paths
    assert parallel_arguments(paths, ["-q"], 2) == (["-q"], "0")
    with pytest.raises(ValueError, match="Parallel execution requires"):
        parallel_arguments(paths, ["-n2"], 2)
