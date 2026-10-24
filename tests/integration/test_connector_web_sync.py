"""Real Web checkbox save, reload and independent scratch SQL readback."""

import os
from pathlib import Path
from typing import Any, Literal

import pytest
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import delete
from sqlalchemy.orm import Session

from api.db.db_models import Connector
from scripts.acceptance.api import ProductAPI, response_data
from scripts.acceptance.browser import WebAcceptance
from scripts.acceptance.evidence import Evidence
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.connector_web import connector_web as connector_web
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.mark.external_consumer
@pytest.mark.parametrize("source", ["dingtalk_ai_table", "imap", "mysql", "postgresql", "moodle"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_connector_web_save_readback(connector_sync_api: dict[str, Any], connector_web: str, source: str, theme: Literal["light", "dark"]) -> None:
    env = connector_sync_api
    api = ProductAPI(env["base"], env["jwt"], env["kb"])
    config = {
        "table_id": "table",
        "operator_id": "operator",
        "batch_size": 100,
        "custom": "preserved",
        "credentials": {"access_token": "synthetic", "custom": "preserved"},
        "sync_deleted_files": False,
    }
    if source == "imap":
        config = {
            "imap_host": "imap.test",
            "imap_port": 993,
            "imap_mailbox": ["INBOX"],
            "poll_range": 30,
            "custom": "preserved",
            "credentials": {"imap_username": "owner", "imap_password": "synthetic", "custom": "preserved"},
            "sync_deleted_files": False,
        }
    if source == "moodle":
        config = {"moodle_url": "https://moodle.invalid", "credentials": {"moodle_token": "synthetic", "custom": "preserved"}, "custom": "preserved", "sync_deleted_files": False}
    if source in {"mysql", "postgresql"}:
        config = {
            "host": "db.test",
            "port": 3306 if source == "mysql" else 5432,
            "database": "owned",
            "query": "SELECT * FROM records",
            "content_columns": "body",
            "metadata_columns": "id",
            "id_column": "id",
            "timestamp_column": "seq",
            "custom": "preserved",
            "credentials": {"username": "owner", "password": "synthetic", "custom": "preserved"},
            "sync_deleted_files": False,
        }
    identifier = api.request("POST", "/connectors", json={"source": source, "name": "Connector scratch", "config": config})["id"]
    evidence = Evidence(Path(os.environ.get("CONNECTOR_WEB_EVIDENCE", ".test-results/connector-web")), mode="web")
    errors: list[str] = []
    browser_flow = WebAcceptance(api, evidence, connector_web, lambda: None)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                with browser_flow._context(browser, theme, (1440, 1000), errors) as context:
                    # Keep this configuration-flow check independent of CDN
                    # availability; rendering uses the declared system fallback.
                    context.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(status=200, content_type="text/css", body="/* controlled system-font fallback */"))
                    page = context.new_page()
                    browser_flow._observe(page, errors, [])
                    route = f"/settings/datasource-detail?id={identifier}"
                    page.goto(connector_web + route, wait_until="networkidle")
                    checkbox = page.get_by_role("checkbox")
                    expect(checkbox).to_have_count(1)
                    assert not checkbox.is_checked()
                    checkbox.focus()
                    page.keyboard.press("Space")
                    assert checkbox.is_checked()
                    with page.expect_response(lambda reply: reply.request.method == "PATCH" and reply.url.endswith(f"/connectors/{identifier}")) as saved:
                        page.get_by_role("button", name="Save", exact=True).click()
                    response_data(saved.value.json(), label="Web connector save", http_status=saved.value.status)
                    assert api.request("GET", f"/connectors/{identifier}")["config"] == {**config, "sync_deleted_files": True}
                    with Session(env["engine"]) as db:
                        assert db.get(Connector, identifier).config == {**config, "sync_deleted_files": True}
                    page.reload(wait_until="networkidle")
                    assert checkbox.is_checked()
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    browser_flow._screenshot(page, source, theme, (1440, 1000), status="save-and-readback-passed")
                    checkbox.scroll_into_view_if_needed()
                    browser_flow._screenshot(page, source + "-deletion", theme, (1440, 1000), status="saved-deletion-switch-visible")
            finally:
                browser.close()
    finally:
        with Session(env["engine"]) as db:
            db.execute(delete(Connector).where(Connector.id == identifier))
            db.commit()
