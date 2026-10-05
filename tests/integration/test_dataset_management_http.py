"""Dataset metadata/ingestions through real HTTP, auth and isolated PostgreSQL."""

import os
import subprocess
from typing import Any

import pytest

from api.db.db_models import Document, Knowledgebase, PipelineOperationLog
from tests.support.dataset_management_http import FIELDS, PARSER, metadata_path, request_api, sql_state
from tests.support.dataset_management_http import management_api as management_api


@pytest.mark.parametrize("route", ["dataset-get", "dataset-put", "document-put", "ingestions"])
@pytest.mark.parametrize("credential", [None, "bad-jwt", "bad-api-key"])
def test_management_real_auth_rejection(management_api: dict[str, Any], route: str, credential: str | None) -> None:
    env = management_api
    before = sql_state(env)
    actual = credential
    if credential == "bad-jwt":
        header, payload_segment, signature = env["jwt"][0].split(".")
        signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        actual = ".".join([header, payload_segment, signature])
    suffix = f"/datasets/{env['datasets'][0]}/ingestions" if route == "ingestions" else metadata_path(env, route == "document-put")
    method = "PUT" if route.endswith("put") else "GET"
    payload = {"metadata": FIELDS}
    response = request_api(env, method, suffix, credential=actual, payload=payload if method == "PUT" else None)
    message = "`Authorization` can't be empty" if credential is None else "Authentication error: invalid credentials!"
    assert response.status_code == 401 and response.json() == {"retcode": 109, "retmsg": message, "data": False}
    assert sql_state(env) == before


@pytest.mark.parametrize("kind", ["jwt", "keys"])
def test_dataset_metadata_real_roundtrip_and_empty_fields(management_api: dict[str, Any], kind: str) -> None:
    env = management_api
    key, path = env[kind][0], metadata_path(env)
    before = sql_state(env)
    assert request_api(env, "GET", path, credential=key).json() == {"code": 0, "data": {"enabled": False, "metadata": [], "built_in_metadata": []}}
    stored = dict(PARSER)
    for config in [{"enabled": True, "metadata": FIELDS}, {}, {"enabled": False}, {"metadata": []}]:
        stored.update({key: value for key, value in config.items() if key != "enabled"})
        if "enabled" in config:
            stored["enable_metadata"] = config["enabled"]
        expected = {"enabled": stored["enable_metadata"], "metadata": stored["metadata"], "built_in_metadata": []}
        response = request_api(env, "PUT", path, credential=key, payload=config)
        assert response.status_code == 200 and response.json() == {"code": 0, "data": expected}
        assert request_api(env, "GET", path, credential=key).json() == response.json()
        after = sql_state(env)
        target = after[Knowledgebase.__tablename__][env["datasets"][0]]
        assert target["parser_config"] == stored
        for dataset in env["datasets"][1:]:
            assert after[Knowledgebase.__tablename__][dataset] == before[Knowledgebase.__tablename__][dataset]
        assert after[Document.__tablename__] == before[Document.__tablename__] and after[PipelineOperationLog.__tablename__] == before[PipelineOperationLog.__tablename__]


@pytest.mark.parametrize("kind", ["jwt", "keys"])
def test_document_metadata_real_array_schema_empty_and_admin(management_api: dict[str, Any], kind: str) -> None:
    env = management_api
    before = sql_state(env)
    path = metadata_path(env, True)
    schema = {"type": "object", "properties": {"author": {"type": "string"}}}
    for value, actor in [(FIELDS, 0), (schema, 0), ([], 2)]:
        response = request_api(env, "PUT", path, credential=env[kind][actor], payload={"metadata": value})
        assert response.status_code == 200 and response.json()["code"] == 0
        assert response.json()["data"]["parser_config"] == {**PARSER, "metadata": value}
        after = sql_state(env)
        assert after[Document.__tablename__][env["documents"][0]]["parser_config"] == {**PARSER, "metadata": value}
        for doc in env["documents"][1:]:
            assert after[Document.__tablename__][doc] == before[Document.__tablename__][doc]
        assert after[Knowledgebase.__tablename__] == before[Knowledgebase.__tablename__] and after[PipelineOperationLog.__tablename__] == before[PipelineOperationLog.__tablename__]


def test_metadata_real_invalid_ownership_and_body(management_api: dict[str, Any]) -> None:
    env = management_api
    before = sql_state(env)
    key, dataset, doc = env["keys"][0], env["datasets"][0], env["documents"][0]
    for method, path, credential, payload, code in [
        ("GET", "/datasets/missing/metadata/config", key, None, 102),
        ("PUT", "/datasets/missing/metadata/config", key, {"metadata": []}, 102),
        ("GET", metadata_path(env), env["jwt"][1], None, 102),
        ("PUT", metadata_path(env), env["keys"][1], {"metadata": FIELDS}, 102),
        ("GET", metadata_path(env), env["keys"][2], None, 102),
        ("PUT", metadata_path(env), env["jwt"][2], {"metadata": FIELDS}, 102),
        ("PUT", f"/datasets/missing/documents/{doc}/metadata/config", key, {"metadata": FIELDS}, 102),
        ("PUT", f"/datasets/{dataset}/documents/missing/metadata/config", key, {"metadata": FIELDS}, 102),
        ("PUT", f"/datasets/{dataset}/documents/{env['documents'][1]}/metadata/config", key, {"metadata": FIELDS}, 102),
        ("PUT", metadata_path(env, True), env["keys"][1], {"metadata": FIELDS}, 109),
    ]:
        response = request_api(env, method, path, credential=credential, payload=payload)
        body = response.json()
        assert response.status_code == 200 and body["code"] == code and isinstance(body["message"], str) and body["message"]
        assert "data" not in body and sql_state(env) == before
    for path, bodies in [(metadata_path(env), [None]), (metadata_path(env, True), [None, {}])]:
        for body in bodies:
            response = request_api(env, "PUT", path, credential=key, payload=body)
            assert response.status_code == 422
            errors = response.json()["detail"]
            assert errors[0]["type"] == "missing" and errors[0]["loc"] == (["body", "metadata"] if body == {} else ["body"])
            assert sql_state(env) == before


@pytest.mark.parametrize(
    "params,positions",
    [
        ({"desc": "false"}, [0, 1, 2]),
        ({"orderby": "create_date", "desc": "true", "page": 2, "page_size": 1}, [1]),
        ({"create_date_from": "2025-01-02", "create_date_to": "2025-01-03", "desc": "false"}, [1, 2]),
        ({"create_date_from": "2025-01-02", "create_date_to": "2025-01-02"}, [1]),
        ({"create_date_from": "2025-01-02", "desc": "false"}, [1, 2]),
        ({"create_date_to": "2025-01-02", "desc": "false"}, [0, 1]),
        ({"operation_status": ["success"]}, [2, 0]),
        ({"create_date_from": "2025-01-02T08:00:00+08:00", "create_date_to": "2025-01-02"}, [1]),
    ],
)
def test_ingestion_logs_real_filters_and_pagination(management_api: dict[str, Any], params: dict[str, Any], positions: list[int]) -> None:
    env = management_api
    before = sql_state(env)
    response = request_api(env, "GET", f"/datasets/{env['datasets'][0]}/ingestions", credential=env["keys"][0], params=params)
    assert response.status_code == 200 and response.json()["code"] == 0
    data = response.json()["data"]
    assert data["total"] == (3 if "page" in params else len(positions))
    assert [row["id"] for row in data["logs"]] == [env["logs"][i] for i in positions]
    assert all(row["kb_id"] == env["datasets"][0] for row in data["logs"])
    assert sql_state(env) == before


def test_ingestion_logs_real_errors_scope_and_smoke(management_api: dict[str, Any]) -> None:
    env = management_api
    before = sql_state(env)
    path, key = f"/datasets/{env['datasets'][0]}/ingestions", env["jwt"][0]
    for start, end in [("2025-02-01", "2025-01-01"), ("2025-01-02T01:00:00Z", "2025-01-02")]:
        response = request_api(env, "GET", path, credential=key, params={"create_date_from": start, "create_date_to": end})
        assert response.status_code == 200 and response.json() == {"code": 102, "message": "create_date_from must not be later than create_date_to"}
    for suffix, credential in [("/datasets/missing/ingestions", key), (path, env["keys"][1])]:
        response = request_api(env, "GET", suffix, credential=credential)
        assert response.status_code == 200 and response.json() == {"code": 102, "message": "No authorization."}
    for params, field in [({"create_date_from": "not-a-date"}, "create_date_from"), ({"page": -1}, "page")]:
        response = request_api(env, "GET", path, credential=key, params=params)
        assert response.status_code == 422 and response.json()["detail"][0]["loc"] == ["query", field]
    response = request_api(env, "GET", path + "/" + env["logs"][-1], credential=key)
    assert response.status_code == 200 and response.json() == {"code": 102, "message": "Log not found"}
    response = request_api(env, "GET", "/datasets//ingestions", credential=key)
    assert response.status_code == 404 and response.json()["code"] == 404
    assert sql_state(env) == before
    smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, capture_output=True, text=True, timeout=60)
    env["record_path"].with_suffix(".smoke.log").write_text(smoke.stdout + smoke.stderr + f"\nexit={smoke.returncode}\n")
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    print("Actual metadata/ingestion HTTP auth and SQL contracts; same listener make smoke passed; exact owned IDs/readback cleanup recorded")


@pytest.mark.parametrize("kind,positions", [("file", [3]), ("dataset", [0, 1, 2])])
def test_ingestion_split_search_fields_and_permissions(management_api: dict[str, Any], kind: str, positions: list[int]) -> None:
    import sqlalchemy as sa

    env = management_api
    path = f"/datasets/{env['datasets'][0]}/ingestions"
    # Change only owned scratch rows; mixed case, Unicode and literal SQL wildcards.
    with env["engine"].begin() as db:
        db.execute(sa.update(PipelineOperationLog).where(PipelineOperationLog.id.in_(env["logs"])).values(document_name="Report_100%报告"))
    before = sql_state(env)
    params = {"log_type": kind, "keywords": "REPORT_100%报告", "desc": "false"}
    for credential in [env["keys"][0], env["jwt"][0]]:
        response = request_api(env, "GET", path, credential=credential, params=params)
        assert response.status_code == 200 and response.json()["code"] == 0
        result = response.json()["data"]
        assert result["total"] == len(positions)
        assert [row["id"] for row in result["logs"]] == [env["logs"][i] for i in positions]
        for row in result["logs"]:
            assert ("document_name" in row) is (kind == "file")
            if kind == "file":
                assert row["document_name"] == "Report_100%报告" and row["document_id"] == env["documents"][0]
                assert row["document_suffix"] == "txt" and row["document_type"] == "txt"
                assert {"pipeline_id", "pipeline_title", "dsl", "parser_id", "source_from"} <= row.keys()
        missing = request_api(env, "GET", path, credential=credential, params={**params, "keywords": "REPORT_100_报告"}).json()
        assert missing["data"] == {"total": 0, "logs": []}
        page = request_api(env, "GET", path, credential=credential, params={**params, "page": 1, "page_size": 1}).json()["data"]
        assert page["total"] == len(positions) and len(page["logs"]) == 1
    denied = request_api(env, "GET", path, credential=env["keys"][1], params=params).json()
    assert denied == {"code": 102, "message": "No authorization."}
    if kind == "file":
        for extra, count in [
            ({"types": ["pdf", "txt"], "suffix": ["txt"]}, 1),
            ({"types": ["pdf"]}, 0),
            ({"suffix": ["pdf"]}, 0),
            ({"create_date_from": "2025-01-04T08:00:00+08:00", "create_date_to": "2025-01-04"}, 1),
            ({"create_date_to": "2025-01-03"}, 0),
            ({"operation_status": ["success"]}, 0),
        ]:
            result = request_api(env, "GET", path, credential=env["keys"][0], params={**params, **extra}).json()
            assert result["code"] == 0 and result["data"]["total"] == count
    assert sql_state(env) == before


@pytest.mark.external_consumer
def test_ingestion_actual_web_consumer(management_api: dict[str, Any]) -> None:
    import json
    from pathlib import Path

    env = management_api
    root = Path(os.environ.get("WEB_DATASET_CHECKOUT", str(Path(__file__).resolve().parents[3] / "web")))
    runner = root / "node_modules/.bin/tsx"
    script = root / "scripts/verify-ingestion-logs.ts"
    assert runner.exists() and script.exists(), "Web checkout required for consumer acceptance"
    before = sql_state(env)
    result = subprocess.run(
        [str(runner), "--tsconfig", str(root / "tsconfig.app.json"), str(script)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
        env={**os.environ, "INGESTION_BASE": env["base"], "INGESTION_TOKEN": env["jwt"][0], "INGESTION_DATASET": env["datasets"][0], "INGESTION_LOG_IDS": json.dumps(env["logs"])},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ingestion Web acceptance passed" in result.stdout
    assert sql_state(env) == before
