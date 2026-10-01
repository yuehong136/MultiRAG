"""Retired API paths stay absent while the file log consumer remains usable."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from api.db.services.pipeline_operation_log_service import PipelineOperationLogService
from common.constants import RetCode

RETIRED_KB_OPERATIONS = [
    ("POST", "/v1/kb/create"),
    ("POST", "/v1/kb/update"),
    ("POST", "/v1/kb/list"),
    ("POST", "/v1/kb/rm"),
    ("GET", "/v1/kb/kb1/knowledge_graph"),
    ("DELETE", "/v1/kb/kb1/knowledge_graph"),
    ("POST", "/v1/kb/run_graphrag"),
    ("GET", "/v1/kb/trace_graphrag"),
    ("POST", "/v1/kb/run_raptor"),
    ("GET", "/v1/kb/trace_raptor"),
]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        *RETIRED_KB_OPERATIONS,
        ("POST", "/v1/evaluation/dataset/create"),
        ("GET", "/v1/evaluation/dataset/list"),
        ("GET", "/v1/evaluation/dataset/dataset1"),
        ("PUT", "/v1/evaluation/dataset/dataset1"),
        ("DELETE", "/v1/evaluation/dataset/dataset1"),
        ("POST", "/v1/evaluation/dataset/dataset1/case/add"),
        ("POST", "/v1/evaluation/dataset/dataset1/case/import"),
        ("GET", "/v1/evaluation/dataset/dataset1/cases"),
        ("DELETE", "/v1/evaluation/case/case1"),
        ("POST", "/v1/evaluation/run/start"),
        ("GET", "/v1/evaluation/run/run1"),
        ("GET", "/v1/evaluation/run/run1/results"),
        ("GET", "/v1/evaluation/run/list"),
        ("DELETE", "/v1/evaluation/run/run1"),
        ("GET", "/v1/evaluation/run/run1/recommendations"),
        ("POST", "/v1/evaluation/compare"),
        ("GET", "/v1/evaluation/run/run1/export"),
    ],
)
def test_retired_operations_return_not_found(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path, json={}).status_code == 404


def test_openapi_omits_retired_operations_and_keeps_replacements(client: TestClient) -> None:
    paths = client.app.openapi()["paths"]
    assert not any(path.startswith("/v1/evaluation") for path in paths)
    for method, path in RETIRED_KB_OPERATIONS:
        schema_path = path.replace("/kb1/", "/{kb_id}/")
        assert method.lower() not in paths.get(schema_path, {})
    for path, methods in {
        "/api/v1/datasets": {"get", "post", "delete"},
        "/api/v1/datasets/{dataset_id}": {"get", "put"},
        "/api/v1/datasets/{dataset_id}/graph/search": {"get"},
        "/api/v1/datasets/{dataset_id}/index": {"get", "post", "delete"},
        "/v1/kb/list_pipeline_logs": {"post"},
    }.items():
        assert methods <= paths[path].keys()


def test_file_logs_keep_the_web_contract(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = [{"id": "log1", "task_type": "download"}]
    requested_kbs: list[str] = []

    def get_logs(_cls: type[PipelineOperationLogService], _db: Session, kb_id: str, *args: Any) -> tuple[list[dict[str, str]], int]:
        requested_kbs.append(kb_id)
        return logs, 1

    monkeypatch.setattr(PipelineOperationLogService, "get_file_logs_by_kb_id", classmethod(get_logs))
    response = client.post("/v1/kb/list_pipeline_logs?kb_id=kb1&page=1&page_size=10", json={"operation_status": []})
    assert response.status_code == 200
    body = response.json()
    assert body["retcode"] == RetCode.SUCCESS
    assert body["data"] == {"logs": logs, "total": 1}
    assert requested_kbs == ["kb1"]


def test_file_logs_still_require_authentication(client: TestClient) -> None:
    from api.apps import manager

    client.app.dependency_overrides.pop(manager)
    response = client.post("/v1/kb/list_pipeline_logs?kb_id=kb1", json={"operation_status": []})
    assert response.status_code == 401
