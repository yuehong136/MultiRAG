"""Real sibling settings page saves against scratch SQL; no synthesized API success."""

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
import sqlalchemy as sa
from playwright.sync_api import expect, sync_playwright
from sqlalchemy.orm import Session

from api.db.db_models import LLM, Knowledgebase, LLMFactories, TenantLLM
from scripts.acceptance.api import ProductAPI, response_data
from scripts.acceptance.browser import WebAcceptance
from scripts.acceptance.evidence import Evidence
from tests.support.document_parse_retirement import parse_api as parse_api
from tests.support.product_acceptance import product_web_server
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


def test_table_settings_real_save_reload_and_locales(parse_api: dict[str, Any]) -> None:
    env = parse_api
    with Session(env["engine"]) as db:
        db.merge(LLMFactories(name="Builtin", tags="Text Embedding", status="1"))
        db.merge(LLM(fid="Builtin", llm_name="scratch-embedding", mdl_type="embedding", max_tokens=8192, tags="Text Embedding", status="1"))
        registered = TenantLLM(tenant_id=env["owners"][0], llm_factory="Builtin", llm_name="scratch-embedding", mdl_type="embedding", api_key="", max_tokens=8192)
        db.add(registered)
        db.flush()
        registered_id = registered.id
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["kb"]).values(tenant_embd_id=registered_id, embd_id="scratch-embedding@Builtin"))
        db.commit()
    config = {"table_column_mode": "manual", "table_column_names": ["title.with.dot", "country", "score"], "table_column_roles": {"title.with.dot": "indexing", "country": "metadata", "score": "both"}}
    result = requests.put(env["base"] + "/api/v1/datasets/" + env["kb"], headers={"Authorization": "Bearer " + env["jwt"]}, json={"chunk_method": "table", "parser_config": config}, timeout=30)
    assert result.status_code == 200 and result.json()["code"] == 0, result.text
    report = Evidence(Path(env["record_path"]).parent / "table-web", mode="full")
    client = ProductAPI(env["base"], env["jwt"], env["kb"])
    report.secrets.append(client.token)
    report.metadata["limits"] = ["Column discovery is seeded here; real parse/storage chain is covered separately by test_table_column_roles. No production or real-provider execution."]
    try:
        with product_web_server(env["base"], report) as base, sync_playwright() as playwright, playwright.chromium.launch(headless=True) as browser:
            view = WebAcceptance(client, report, base, lambda: None)
            for theme in ["light", "dark"]:
                errors: list[str] = []
                with view._context(browser, theme, (1440, 1000), errors) as context:
                    context.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(status=200, content_type="text/css", body=""))
                    page = context.new_page()
                    view._observe(page, errors, [])
                    view._navigate(page, f"/knowledge/{env['kb']}/settings")
                    country = page.get_by_role("combobox", name="country", exact=True)
                    expect(country).to_be_visible()
                    country.focus()
                    page.keyboard.press("Enter")
                    page.get_by_role("option", name="Both", exact=True).click()
                    with page.expect_response(lambda reply: reply.request.method == "PUT" and urlsplit(reply.url).path.endswith("/datasets/" + env["kb"])) as captured:
                        page.get_by_role("button", name="Save", exact=True).click()
                    report.metadata["save_response"] = captured.value.json()
                    report.save()
                    response_data(captured.value.json(), label="Web table save", http_status=captured.value.status)
                    with Session(env["engine"]) as db:
                        stored = db.scalar(sa.select(Knowledgebase.parser_config).where(Knowledgebase.id == env["kb"]))
                        assert stored["table_column_roles"]["country"] == "both"
                        assert stored["table_column_roles"]["title.with.dot"] == "indexing"
                    page.reload(wait_until="networkidle")
                    expect(page.get_by_role("combobox", name="country", exact=True)).to_have_text("Both")
                    page.get_by_role("combobox", name="country", exact=True).scroll_into_view_if_needed()
                    view._screenshot(page, "table-settings-en", theme, (1440, 1000), status="Saved, reloaded; keyboard selection verified")
                    # Locale preference is UI-owned; business data and protocol values stay unchanged.
                    page.evaluate("async () => { const { setProductLanguage } = await import('/src/locales/i18n.ts'); await setProductLanguage('zh-CN'); }")
                    expect(page.get_by_role("combobox", name="列配置模式", exact=True)).to_be_visible()
                    expect(page.get_by_role("combobox", name="country", exact=True)).to_have_text("两者")
                    page.get_by_role("combobox", name="country", exact=True).scroll_into_view_if_needed()
                    view._screenshot(page, "table-settings-zh", theme, (1440, 1000), status="Chinese locale; same saved roles")
                    report.results.append({"name": f"web.{theme}.table_save_readback", "status": "passed", "detail": {"metadata": stored, "locales": ["en", "zh"]}, "seconds": 0})
                    report.save()
        report.contact_sheets()
        report.finish()
        assert report.successful
    finally:
        client.session.close()
        with env["engine"].begin() as db:
            db.execute(sa.delete(TenantLLM).where(TenantLLM.id == registered_id))
