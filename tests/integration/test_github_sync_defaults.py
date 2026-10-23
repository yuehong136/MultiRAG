"""GitHub defaults through real REST persistence, finite sync and Web forms."""

import json
import os
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest
import requests
import sqlalchemy as sa
from playwright.sync_api import expect, sync_playwright
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, File, File2Document, SyncLogs, Task
from common.constants import TaskStatus
from core.svr.sync_data_source import Github
from scripts.acceptance.api import ProductAPI, response_data
from scripts.acceptance.browser import WebAcceptance
from scripts.acceptance.evidence import Evidence
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api
from tests.unit.test_github_sync_defaults import GithubTransport, github_config
from tests.unit.test_github_sync_defaults import github_transport as github_transport


@pytest.fixture(scope="module")
def github_web(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    checkout = Path(os.environ.get("WEB_DATASET_CHECKOUT", str(Path(__file__).resolve().parents[3] / "web")))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    evidence = Path(os.environ.get("CONNECTOR_WEB_EVIDENCE", str(tmp_path_factory.mktemp("github_web"))))
    evidence.mkdir(parents=True, exist_ok=True)
    with (evidence / "vite.log").open("w") as log:
        # Wait on Vite itself so npm exiting cannot race listener shutdown.
        process = subprocess.Popen(
            ["node", "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", str(port), "--strictPort"], cwd=checkout, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    if requests.get(base, timeout=1).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Owned GitHub Web server failed to start")
            yield base
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
            with socket.socket() as listener:
                closed = listener.connect_ex(("127.0.0.1", port)) != 0
            (evidence / "web-server-cleanup.json").write_text(json.dumps({"pid": process.pid, "port": port, "exit": process.returncode, "listener_closed": closed}))
            assert closed


def saved_config(env: dict[str, Any], api: ProductAPI, identifier: str) -> dict[str, Any]:
    config = api.request("GET", f"/connectors/{identifier}")["config"]
    with Session(env["engine"]) as db:
        stored = db.get(Connector, identifier)
        assert stored is not None and stored.config == config
    return config


def cleanup_github(env: dict[str, Any], identifiers: list[str]) -> None:
    """Remove only this fixture's connectors/documents, then read exact IDs back."""
    with Session(env["engine"]) as db:
        doc_ids = list(db.scalars(sa.select(Document.id).where(Document.kb_id == env["kb"])))
        file_ids = list(db.scalars(sa.select(File.id).where(File.tenant_id == env["owners"][0])))
        task_ids = list(db.scalars(sa.select(SyncLogs.id).where(SyncLogs.connector_id.in_(identifiers))))
        conditions = [
            (Task, Task.doc_id.in_(doc_ids)),
            (File2Document, File2Document.document_id.in_(doc_ids)),
            (Document, Document.id.in_(doc_ids)),
            (File, File.id.in_(file_ids)),
            (SyncLogs, SyncLogs.id.in_(task_ids)),
            (Connector2Kb, Connector2Kb.connector_id.in_(identifiers)),
            (Connector, Connector.id.in_(identifiers)),
        ]
        for model, condition in conditions:
            db.execute(sa.delete(model).where(condition))
        db.commit()
    with env["engine"].connect() as db:
        remaining = {model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model).where(condition)) for model, condition in conditions}
    env["record"]["github"] = {"connectors": identifiers, "documents": doc_ids, "files": file_ids, "sync_tasks": task_ids, "collection": env["collection"], "remaining": remaining}
    assert not any(remaining.values()), remaining


@pytest.mark.parametrize(
    "options,status,document_count",
    [
        ({}, 200, 4),
        ({"include_pull_requests": True, "include_issues": True}, 200, 4),
        ({"include_pull_requests": False, "include_issues": False}, 200, 0),
        ({}, 403, 0),
    ],
)
async def test_github_rest_saved_config_finite_sync_and_denied_side_effects(
    connector_sync_api: dict[str, Any], github_transport: GithubTransport, options: dict[str, bool], status: int, document_count: int
) -> None:
    from api.apps import manager

    env = connector_sync_api
    api = ProductAPI(env["base"], env["jwt"], env["kb"])
    config = github_config(**options)
    identifier = api.request("POST", "/connectors", json={"name": "GitHub default contract", "source": "github", "config": config})["id"]
    driver = Github(config)
    try:
        assert saved_config(env, api, identifier) == config
        assert github_transport.calls == [], "Saving a source must not perform a provider probe"
        other_jwt = manager.create_access_token(data={"sub": f"{env['owners'][1]}@upload.test"})
        endpoint = env["base"] + f"/api/v1/connectors/{identifier}"
        for method, kwargs in [("GET", {}), ("PATCH", {"json": {"config": {"include_issues": False}}})]:
            reply = requests.request(method, endpoint, headers={"Authorization": f"Bearer {other_jwt}"}, timeout=30, **kwargs)
            assert reply.status_code in {200, 403}
            assert reply.json()["retcode"] != 0
        assert saved_config(env, api, identifier) == config
        assert github_transport.calls == []
        api.request("PUT", f"/datasets/{env['kb']}/connectors/{identifier}", json={"auto_parse": False})
        with Session(env["engine"]) as db:
            log = db.scalar(sa.select(SyncLogs).where(SyncLogs.connector_id == identifier))
            assert log is not None
            current = {"id": log.id, "connector_id": identifier, "kb_id": env["kb"], "tenant_id": env["owners"][0], "poll_range_start": None, "reindex": "0", "auto_parse": False, "timeout_secs": 30}
        github_transport.status = status
        # Read the exact persisted configuration used by the registered sync driver.
        driver = Github(saved_config(env, api, identifier))
        await driver(current)
        with Session(env["engine"]) as db:
            log = db.get(SyncLogs, current["id"])
            assert log is not None and log.status == (TaskStatus.DONE if status == 200 else TaskStatus.FAIL), log.full_exception_trace if log else "missing log"
            documents = list(db.scalars(sa.select(Document).where(Document.kb_id == env["kb"])))
            assert len(documents) == document_count
            assert len({document.id for document in documents}) == document_count
            for document in documents:
                assert document.source_type == f"github/{identifier}"
                assert read_object(env["storage"], env["bucket"], f"{env['kb']}/{document.location}").startswith(b"Controlled ")
            if status != 200:
                assert db.scalar(sa.select(sa.func.count()).select_from(SyncLogs).where(SyncLogs.connector_id == identifier)) == 1
        assert len(list(env["storage"].list_objects(env["bucket"], recursive=True))) == document_count
        assert saved_config(env, api, identifier) == config
        env["record"]["provider_requests"] = github_transport.calls
        env["record"]["sync_status"] = "DONE" if status == 200 else "FAIL"
        env["record"]["synced_documents"] = document_count
    finally:
        if hasattr(driver, "connector") and driver.connector.github_client is not None:
            driver.connector.github_client.close()
        cleanup_github(env, [identifier])


@pytest.mark.external_consumer
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_github_real_web_create_edit_and_legacy_readback(connector_sync_api: dict[str, Any], github_web: str, theme: Literal["light", "dark"]) -> None:
    env = connector_sync_api
    api = ProductAPI(env["base"], env["jwt"], env["kb"])
    root = Path(os.environ.get("CONNECTOR_WEB_EVIDENCE", ".test-results/github-web"))
    evidence = Evidence(root / theme, mode="web")
    evidence.metadata.update(
        embedding="not exercised; connector configuration only",
        visual_review="pending_contact_sheet_review",
        limits=["GitHub provider HTTP is controlled separately; no live GitHub data or production changes."],
    )
    browser_flow = WebAcceptance(api, evidence, github_web, lambda: None)
    errors: list[str] = []
    identifiers: list[str] = []
    payloads: list[dict[str, Any]] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                with browser_flow._context(browser, theme, (1440, 1000), errors) as context:
                    context.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(status=200, content_type="text/css", body="/* controlled system-font fallback */"))
                    page = context.new_page()
                    browser_flow._observe(page, errors, [])
                    page.goto(github_web + "/settings/datasource", wait_until="networkidle")
                    page.get_by_role("heading", name="GitHub", exact=True).click()
                    dialog = page.get_by_role("dialog")
                    expect(dialog).to_be_visible()
                    for name, value in [
                        ("name", "GitHub browser defaults"),
                        ("config.repository_owner", "fixture"),
                        ("config.repository_name", "project"),
                        ("config.credentials.github_access_token", "synthetic-github-token"),
                    ]:
                        dialog.locator(f'input[name="{name}"]').fill(value)
                    prs = page.get_by_role("checkbox", name="Include Pull Requests", exact=True)
                    issues = page.get_by_role("checkbox", name="Include Issues", exact=True)
                    expect(prs).to_be_checked()
                    expect(issues).to_be_checked()
                    browser_flow._screenshot(page, "github-create-defaults", theme, (1440, 1000), status="both-defaults-checked")
                    with page.expect_response(lambda reply: reply.request.method == "POST" and reply.url.endswith("/connectors")) as created:
                        dialog.get_by_role("button", name="Confirm", exact=True).click()
                    identifier = response_data(created.value.json(), label="GitHub browser create", http_status=created.value.status)["id"]
                    identifiers.append(identifier)
                    payloads.append(created.value.request.post_data_json["config"])
                    assert saved_config(env, api, identifier) == payloads[-1]
                    assert payloads[-1]["include_pull_requests"] is True and payloads[-1]["include_issues"] is True
                    page.goto(github_web + f"/settings/datasource-detail?id={identifier}", wait_until="networkidle")
                    expect(prs).to_be_checked()
                    expect(issues).to_be_checked()
                    prs.focus()
                    page.keyboard.press("Space")
                    issues.focus()
                    page.keyboard.press("Space")
                    with page.expect_response(lambda reply: reply.request.method == "PATCH" and reply.url.endswith(f"/connectors/{identifier}")) as saved:
                        page.get_by_role("button", name="Save", exact=True).click()
                    response_data(saved.value.json(), label="GitHub browser edit", http_status=saved.value.status)
                    payloads.append(saved.value.request.post_data_json["config"])
                    assert saved_config(env, api, identifier) == payloads[-1]
                    assert payloads[-1]["include_pull_requests"] is False and payloads[-1]["include_issues"] is False
                    page.reload(wait_until="networkidle")
                    expect(prs).not_to_be_checked()
                    expect(issues).not_to_be_checked()
                    browser_flow._screenshot(page, "github-edit-disabled", theme, (1440, 1000), status="explicit-false-saved-and-reloaded")
                    legacy_config = {**github_config(), "custom": "preserved"}
                    legacy_id = api.request("POST", "/connectors", json={"name": "GitHub legacy missing flags", "source": "github", "config": legacy_config})["id"]
                    identifiers.append(legacy_id)
                    assert saved_config(env, api, legacy_id) == legacy_config
                    page.goto(github_web + f"/settings/datasource-detail?id={legacy_id}", wait_until="networkidle")
                    expect(prs).to_be_checked()
                    expect(issues).to_be_checked()
                    assert saved_config(env, api, legacy_id) == legacy_config, "Loading an old source must not mutate its SQL config"
                    with page.expect_response(lambda reply: reply.request.method == "PATCH" and reply.url.endswith(f"/connectors/{legacy_id}")) as legacy_saved:
                        page.get_by_role("button", name="Save", exact=True).click()
                    response_data(legacy_saved.value.json(), label="GitHub legacy explicit save", http_status=legacy_saved.value.status)
                    payloads.append(legacy_saved.value.request.post_data_json["config"])
                    assert saved_config(env, api, legacy_id) == {**legacy_config, "include_pull_requests": True, "include_issues": True, "sync_deleted_files": False}
                    page.reload(wait_until="networkidle")
                    expect(prs).to_be_checked()
                    expect(issues).to_be_checked()
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    browser_flow._screenshot(page, "github-legacy-enabled", theme, (1440, 1000), status="missing-flags-use-runtime-defaults-and-save-explicitly")
            finally:
                browser.close()
        evidence.results.append(
            {
                "name": "real-Web-create-edit-legacy-SQL-readback",
                "status": "passed",
                "payload_flags": [{key: config[key] for key in ("include_pull_requests", "include_issues")} for config in payloads],
            }
        )
        evidence.completed = True
        evidence.save()
        env["record"]["browser"] = {
            "theme": theme,
            "route": "/settings/datasource",
            "payload_flags": [{key: config[key] for key in ("include_pull_requests", "include_issues")} for config in payloads],
        }
    finally:
        cleanup_github(env, identifiers)
