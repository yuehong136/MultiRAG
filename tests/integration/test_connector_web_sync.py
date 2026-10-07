"""Real Web checkbox save, reload and independent scratch SQL readback."""

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
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import delete
from sqlalchemy.orm import Session

from api.db.db_models import Connector
from scripts.acceptance.api import ProductAPI, response_data
from scripts.acceptance.browser import WebAcceptance
from scripts.acceptance.evidence import Evidence
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture(scope="module")
def connector_web(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    checkout = Path(os.environ.get("WEB_DATASET_CHECKOUT", str(Path(__file__).resolve().parents[3] / "web")))
    assert (checkout / "node_modules").is_dir(), "Installed Web checkout is required"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("connector_web") / "vite.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port), "--strictPort"], cwd=checkout, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
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
                pytest.fail("Owned Web server failed to start")
            yield base
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
            with socket.socket() as listener:
                assert listener.connect_ex(("127.0.0.1", port)) != 0
            evidence_dir = Path(os.environ.get("CONNECTOR_WEB_EVIDENCE", ".test-results/connector-web"))
            evidence_dir.mkdir(parents=True, exist_ok=True)
            (evidence_dir / "web-server-cleanup.json").write_text(json.dumps({"pid": process.pid, "port": port, "listener_closed": True}))


@pytest.mark.external_consumer
@pytest.mark.parametrize("source", ["dingtalk_ai_table"])
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
    identifier = api.request("POST", "/connectors", json={"source": source, "name": "Connector scratch", "config": config})["id"]
    evidence = Evidence(Path(os.environ.get("CONNECTOR_WEB_EVIDENCE", ".test-results/connector-web")), mode="web")
    errors: list[str] = []
    browser_flow = WebAcceptance(api, evidence, connector_web, lambda: None)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                with browser_flow._context(browser, theme, (1440, 1000), errors) as context:
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
            finally:
                browser.close()
    finally:
        with Session(env["engine"]) as db:
            db.execute(delete(Connector).where(Connector.id == identifier))
            db.commit()
