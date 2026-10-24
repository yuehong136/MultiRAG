"""Actual fixed Web consumer against registered HTTP routes and scratch SQL."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from playwright.sync_api import expect, sync_playwright
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, SyncLogs, UserTenant
from common.constants import ConnectorTaskType, TaskStatus
from scripts.acceptance.api import ProductAPI, response_data
from scripts.acceptance.browser import WebAcceptance
from scripts.acceptance.evidence import Evidence
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.connector_web import connector_web as connector_web
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.mark.external_consumer
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_dashboard_patch_actions_validation_and_sql_readback(connector_sync_api: dict[str, Any], connector_web: str, theme: Literal["light", "dark"]) -> None:
    env = connector_sync_api
    api = ProductAPI(env["base"], env["jwt"], env["kb"])
    config = {"base_url": "https://dav.invalid", "remote_path": "/owned", "credentials": {"username": "synthetic", "password": "synthetic"}, "sync_deleted_files": False, "custom": "keep"}
    identifier = api.request("POST", "/connectors", json={"name": "Connector dashboard", "source": "webdav", "config": config})["id"]
    endpoint = env["base"] + f"/api/v1/connectors/{identifier}"
    owner_headers = {"Authorization": "Bearer " + env["jwt"]}
    foreign_headers = {"Authorization": "Bearer " + env["api_key"]}
    member_id = uuid4().hex
    other_id: str | None = None
    evidence = Evidence(Path(os.environ.get("CONNECTOR_WEB_EVIDENCE", ".test-results/connector-dashboard")) / theme, mode="web")
    evidence.metadata.update(embedding="No model calls; connector HTTP and dashboard controls only", limits=["Controlled save failure; no external WebDAV or OAuth provider request."])
    browser_flow = WebAcceptance(api, evidence, connector_web, lambda: None)
    requests_seen: list[dict[str, Any]] = []

    def sql_state() -> tuple[Any, ...]:
        with Session(env["engine"]) as db:
            connector = db.get(Connector, identifier)
            logs = list(
                db.execute(
                    sa.select(SyncLogs.id, SyncLogs.task_type, SyncLogs.status, SyncLogs.poll_range_start, SyncLogs.poll_range_end).where(SyncLogs.connector_id == identifier).order_by(SyncLogs.id)
                )
            )
            return connector.config, connector.refresh_freq, connector.prune_freq, connector.timeout_secs, connector.status, logs

    try:
        initial = sql_state()
        assert initial[4] == TaskStatus.UNSTART
        assert requests.patch(endpoint, headers=foreign_headers, json={"status": "5"}, timeout=10).json()["retcode"] == 109
        for payload in ({"refresh_freq": True}, {"prune_freq": 1.5}, {"timeout_secs": 0}, {"status": "3"}, {"data": {"status": "5"}}):
            assert requests.patch(endpoint, headers=owner_headers, json=payload, timeout=10).status_code == 422
            assert sql_state() == initial
        invalid = requests.patch(endpoint, headers=owner_headers, json={"status": "2", "reschedule": True}, timeout=10)
        assert invalid.json()["retcode"] != 0 and sql_state() == initial
        assert requests.post(endpoint + "/resume", headers=owner_headers, json={"resume": True}, timeout=10).status_code == 404
        assert requests.put(env["base"] + f"/v1/connector/{identifier}/resume", headers=owner_headers, json={"resume": True}, timeout=10).status_code == 404
        assert sql_state() == initial

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                errors: list[str] = []
                with browser_flow._context(browser, theme, (1440, 1000), errors) as context:
                    context.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(status=200, content_type="text/css", body="/* controlled system fonts */"))
                    page = context.new_page()
                    browser_flow._observe(page, errors, [])
                    page.on(
                        "request",
                        lambda request: (
                            requests_seen.append(
                                {"method": request.method, "path": request.url.split("?", 1)[0], "fields": sorted((request.post_data_json or {}).keys()) if request.method == "PATCH" else []}
                            )
                            if "/connectors/" in request.url
                            else None
                        ),
                    )
                    page.goto(connector_web + f"/settings/datasource-detail?id={identifier}", wait_until="networkidle")
                    expect(page.get_by_role("button", name="Save", exact=True)).to_be_visible()
                    expect(page.locator('input[name="prune_freq"]')).to_have_count(0)
                    page.get_by_role("checkbox").check()
                    page.locator('input[name="refresh_freq"]').fill("7")
                    page.locator('input[name="prune_freq"]').fill("19")
                    page.locator('input[name="timeout_secs"]').fill("30")
                    with page.expect_response(lambda response: response.request.method == "PATCH" and response.url.endswith(f"/connectors/{identifier}")) as saved:
                        page.get_by_role("button", name="Save", exact=True).click()
                    response_data(saved.value.json(), label="connector save", http_status=saved.value.status)
                    expect(page.get_by_role("button", name="Stop", exact=True)).to_be_visible()
                    state = sql_state()
                    for key, value in {**config, "sync_deleted_files": True}.items():
                        assert state[0][key] == value
                    assert state[1:4] == (7, 19, 30)
                    api.request("PUT", f"/datasets/{env['kb']}/connectors/{identifier}", json={"auto_parse": False})
                    page.reload(wait_until="networkidle")
                    expect(page.get_by_role("cell", name="Sync", exact=True)).to_be_visible()
                    expect(page.get_by_role("cell", name="Prune", exact=True)).to_be_visible()
                    page.get_by_role("button", name="Stop", exact=True).focus()
                    page.keyboard.press("Enter")
                    expect(page.get_by_role("button", name="Resume", exact=True)).to_be_visible()
                    assert sql_state()[4] == TaskStatus.CANCEL
                    page.get_by_role("button", name="Resume", exact=True).click()
                    expect(page.get_by_role("button", name="Stop", exact=True)).to_be_visible()
                    page.locator('input[name="refresh_freq"]').fill("8")
                    expect(page.get_by_role("button", name="Save", exact=True)).to_be_visible()

                    def fail_save(route: Any) -> None:
                        if route.request.method == "PATCH":
                            route.fulfill(status=503, content_type="application/json", body='{"retcode":100,"retmsg":"controlled save failure"}')
                        else:
                            route.fallback()

                    pattern = f"**/connectors/{identifier}"
                    page.route(pattern, fail_save)
                    page.get_by_role("button", name="Save", exact=True).click()
                    expect(page.get_by_role("button", name="Save", exact=True)).to_be_enabled()
                    expect(page.locator('input[name="refresh_freq"]')).to_have_value("8")
                    assert sql_state()[1] == 7
                    page.unroute(pattern, fail_save)
                    page.get_by_role("button", name="Save", exact=True).click()
                    expect(page.get_by_role("button", name="Stop", exact=True)).to_be_visible()
                    assert sql_state()[1] == 8
                    with Session(env["engine"]) as db:
                        active = list(db.scalars(sa.select(SyncLogs).where(SyncLogs.connector_id == identifier, SyncLogs.status == TaskStatus.SCHEDULE)))
                        assert {row.task_type for row in active} == set(ConnectorTaskType) and len(active) == 2
                    assert not any(request["path"].endswith("/resume") for request in requests_seen)
                    other_id = api.request(
                        "POST",
                        "/connectors",
                        json={
                            "name": "Other connector",
                            "source": "webdav",
                            "config": {**config, "remote_path": "/other-guard", "credentials": {"username": "other", "password": "other-secret"}},
                            "refresh_freq": 13,
                        },
                    )["id"]
                    page.evaluate("url => { history.pushState({}, '', url); dispatchEvent(new PopStateEvent('popstate')); }", f"/settings/datasource-detail?id={other_id}")
                    expect(page.locator('input[name="config.remote_path"]')).to_have_value("/other-guard")
                    expect(page.locator('input[name="config.credentials.password"]')).to_have_value("other-secret")
                    expect(page.locator('input[name="refresh_freq"]')).to_have_value("13")
                    page.evaluate("url => { history.pushState({}, '', url); dispatchEvent(new PopStateEvent('popstate')); }", f"/settings/datasource-detail?id={identifier}")
                    expect(page.locator('input[name="config.remote_path"]')).to_have_value("/owned")
                    expect(page.locator('input[name="refresh_freq"]')).to_have_value("8")
                    with Session(env["engine"]) as db:
                        for kind in ConnectorTaskType:
                            db.add(
                                SyncLogs(
                                    id=uuid4().hex,
                                    connector_id=identifier,
                                    kb_id=env["kb"],
                                    task_type=kind,
                                    status=TaskStatus.DONE,
                                    time_started=datetime.now(UTC) - timedelta(minutes=2),
                                    new_docs_indexed=1 if kind == ConnectorTaskType.SYNC else 0,
                                    total_docs_indexed=3 if kind == ConnectorTaskType.SYNC else 0,
                                    docs_removed_from_index=2 if kind == ConnectorTaskType.PRUNE else 0,
                                )
                            )
                        db.execute(
                            sa.update(SyncLogs)
                            .where(SyncLogs.connector_id == identifier, SyncLogs.task_type == ConnectorTaskType.PRUNE, SyncLogs.status == TaskStatus.SCHEDULE)
                            .values(update_date=datetime.now(UTC).replace(tzinfo=None))
                        )
                        db.commit()
                    page.reload(wait_until="networkidle")
                    expect(page.locator(".ant-message-notice, [data-sonner-toast]")).to_have_count(0, timeout=10000)
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    browser_flow._screenshot(page, "connector-dashboard", theme, (1440, 1000), status="actions-and-readback-passed")
                    page.get_by_text("Sync history", exact=True).scroll_into_view_if_needed()
                    expect(page.get_by_text("Removed 2 documents · 0 errors", exact=True)).to_be_visible()
                    browser_flow._screenshot(page, "connector-dashboard-logs", theme, (1440, 1000), status="task-types-countdown-and-summaries-passed")
                    if theme == "dark":
                        page.set_viewport_size({"width": 390, "height": 844})
                        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                        page.get_by_role("button", name="Stop", exact=True).scroll_into_view_if_needed()
                        expect(page.get_by_role("button", name="Stop", exact=True)).to_be_visible()
                        browser_flow._screenshot(page, "connector-dashboard-mobile", theme, (390, 844), status="responsive-actions-passed")
            finally:
                browser.close()

        # A valid tenant member can use the same status contract without admin privileges.
        with Session(env["engine"]) as db:
            db.add(UserTenant(id=member_id, user_id=env["owners"][1], tenant_id=env["owners"][0], invited_by=env["owners"][0], role="normal", status="1"))
            db.commit()
        member = requests.patch(endpoint, headers=foreign_headers, json={"status": "2"}, timeout=10)
        assert member.status_code == 200 and member.json()["retcode"] == 0
        assert sql_state()[4] == TaskStatus.CANCEL
        evidence.results.append({"name": "connector-dashboard", "status": "passed", "theme": theme, "requests": requests_seen, "sql_readback": True, "member_allowed": True, "foreign_denied": True})
        evidence.contact_sheets()
        evidence.finish()
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(UserTenant).where(UserTenant.id == member_id))
            identifiers = [identifier, other_id] if other_id else [identifier]
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id.in_(identifiers)))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.connector_id.in_(identifiers)))
            db.execute(sa.delete(Connector).where(Connector.id.in_(identifiers)))
            db.commit()
