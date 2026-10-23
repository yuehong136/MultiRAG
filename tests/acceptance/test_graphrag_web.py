"""Real sibling Web pause/restart requests against owned storage and API resources."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import sqlalchemy as sa
from playwright.sync_api import expect, sync_playwright

from api.db.db_models import Knowledgebase
from scripts.acceptance.api import ProductAPI, response_data
from scripts.acceptance.browser import WebAcceptance
from scripts.acceptance.evidence import Evidence
from tests.support.dataset_management_http import management_api as management_api
from tests.support.graphrag_acceptance import DocumentExtractor, Model, api, observe, worker
from tests.support.graphrag_acceptance import graphrag_api as graphrag_api
from tests.support.product_acceptance import product_web_server


async def test_web_delete_graph_refreshes_task_and_persisted_state(graphrag_api: dict[str, Any]) -> None:
    env = graphrag_api
    loop = asyncio.get_running_loop()
    report = Evidence(Path(env["graph_record_path"]).parent / "delete-web", mode="full")
    report.metadata.update(limits=["Controlled extraction/model responses; real Web, registered API, SQL, Redis and Milvus."], backend_base_sha="24200323053904c31257e681c01881f19e3337fd")
    client = ProductAPI(env["base"], env["jwt"][0], env["kb"])
    report.secrets.append(client.token)

    def browser() -> None:
        with product_web_server(env["base"], report) as base, sync_playwright() as playwright, playwright.chromium.launch(headless=True) as browser:
            view = WebAcceptance(client, report, base, lambda: None)
            for theme in ["light", "dark"]:
                task_id = api(env, "POST")["data"]["task_id"]
                asyncio.run_coroutine_threadsafe(worker(env, task_id, Model()), loop).result(timeout=45)
                # Mirror the outer executor completion field after the actual
                # graph entry point has persisted its task and native rows.
                with env["engine"].begin() as db:
                    db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["kb"]).values(graphrag_task_finish_at=datetime(2026, 1, 1)))
                before = observe(env, f"Web {theme} completed before clear")
                assert before["dataset"]["graphrag_task_finish_at"]
                errors: list[str] = []
                with view._context(browser, theme, (1440, 1000), errors) as context:
                    context.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(status=200, content_type="text/css", body=""))
                    page = context.new_page()
                    view._navigate(page, f"/knowledge/{env['kb']}/documents")
                    page.get_by_role("button", name="Generate", exact=True).click()
                    remove = page.get_by_role("button", name="Delete generated result", exact=True)
                    expect(remove).to_be_visible()
                    view._screenshot(page, "graph-before-delete", theme, (1440, 1000), status="completed graph")
                    remove.click()
                    with page.expect_response(lambda reply: reply.request.method == "DELETE" and urlsplit(reply.url).path.endswith("/index")) as captured:
                        confirm = page.get_by_role("dialog").get_by_role("button", name="Delete", exact=True)
                        if theme == "dark":
                            confirm.focus()
                            page.keyboard.press("Enter")
                        else:
                            confirm.click()
                    reply = captured.value
                    assert parse_qs(urlsplit(reply.url).query)["type"] == ["graph"]
                    assert response_data(reply.json(), label="Web graph clear", http_status=reply.status) == {}
                    expect(remove).to_have_count(0)
                    if page.get_by_role("button", name="Start generation", exact=True).count() != 2:
                        page.get_by_role("button", name="Generate", exact=True).click()
                    expect(page.get_by_role("button", name="Start generation", exact=True)).to_have_count(2)
                    after = observe(env, f"Web {theme} cleared")
                    assert after["dataset"]["graphrag_task_id"] == "" and after["dataset"]["graphrag_task_finish_at"] is None
                    assert after["task_rows"] == [] and not any(after["markers"].values())
                    assert len(after["rows"]) == 1 and after["rows"][0].get("knowledge_graph_kwd") is None
                    assert api(env, "GET") == {"code": 0, "data": {}}
                    assert client.request("GET", f"/datasets/{env['kb']}/graph") == {"graph": {}, "mind_map": {}}
                    page.reload(wait_until="networkidle")
                    page.get_by_role("button", name="Generate", exact=True).click()
                    expect(page.get_by_role("button", name="Delete generated result", exact=True)).to_have_count(0)
                    view._screenshot(page, "graph-after-delete", theme, (1440, 1000), status="cleared task after reload")
                    assert errors == [], errors
                    report.results.append(
                        {
                            "name": f"web.{theme}.delete_graph_readback",
                            "status": "passed",
                            "detail": {"binding_cleared": True, "finish_cleared": True, "source_retained": True, "reload_empty": True},
                            "seconds": 0,
                        }
                    )
                    report.save()

    try:
        await asyncio.to_thread(browser)
        report.contact_sheets()
        report.finish()
        assert report.successful
    finally:
        client.session.close()
        report.save()


async def test_web_pause_restart_preserves_checkpoints_and_counts(graphrag_api: dict[str, Any]) -> None:
    env = graphrag_api
    model = Model()
    await worker(env, api(env, "POST")["data"]["task_id"], model)
    loop = asyncio.get_running_loop()
    report = Evidence(Path(env["graph_record_path"]).parent / "web", mode="full")
    report.metadata.update(limits=["Controlled extraction and model responses; real GraphRAG entry point, Web, SQL, Redis and Milvus."], backend_base_sha="044805a671bb2565f8ae385fea5cbcef4edb4f82")
    client = ProductAPI(env["base"], env["jwt"][0], env["kb"])
    report.secrets.append(client.token)

    def browser() -> None:
        with product_web_server(env["base"], report) as base, sync_playwright() as playwright, playwright.chromium.launch(headless=True) as browser:
            view = WebAcceptance(client, report, base, lambda: None)
            for theme in ["light", "dark"]:
                task_id = api(env, "POST")["data"]["task_id"]
                errors: list[str] = []
                cancelled: list[str] = []
                before = observe(env, f"Web {theme} before pause")
                with view._context(browser, theme, (1440, 1000), errors) as context:
                    # External font CDN is an asset-only controlled boundary.
                    # Business requests still relay to the owned API verbatim.
                    context.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(status=200, content_type="text/css", body=""))
                    page = context.new_page()
                    view._observe(page, errors, cancelled)
                    route = f"/knowledge/{env['kb']}/documents"
                    view._navigate(page, route)
                    pause = page.get_by_role("button", name="Stop task", exact=True)
                    expect(pause).to_be_visible()
                    view._screenshot(page, "graph-running", theme, (1440, 1000), status="running queued task")
                    with page.expect_response(lambda reply: reply.request.method == "DELETE" and urlsplit(reply.url).path.endswith("/index")) as captured:
                        pause.focus()
                        page.keyboard.press("Enter")
                    reply = captured.value
                    response_data(reply.json(), label="Web graph pause", http_status=reply.status)
                    query = parse_qs(urlsplit(reply.url).query)
                    assert query["wipe"] == ["false"] and query["task_id"] == [task_id]
                    assert env["redis"].get(task_id + "-cancel")
                    paused = observe(env, f"Web {theme} paused")
                    assert paused["rows"] == before["rows"] and paused["markers"] == before["markers"]
                    assert paused["dataset"]["graphrag_task_id"] == task_id
                    retry = page.get_by_role("button", name="Retry", exact=True)
                    expect(retry).to_be_visible()
                    view._screenshot(page, "graph-paused", theme, (1440, 1000), status="checkpoint preserved")
                    with page.expect_response(lambda reply: reply.request.method == "POST" and urlsplit(reply.url).path.endswith("/index")) as restart:
                        retry.click()
                    new_task = response_data(restart.value.json(), label="Web graph restart", http_status=restart.value.status)["task_id"]
                    assert new_task != task_id
                    asyncio.run_coroutine_threadsafe(worker(env, new_task, model), loop).result(timeout=45)
                    page.reload(wait_until="networkidle")
                    expect(page.get_by_role("button", name="Stop task", exact=True)).to_have_count(0)
                    view._screenshot(page, "graph-resumed", theme, (1440, 1000), status="completed from checkpoints")
                    assert DocumentExtractor.calls == [env["documents"][0]]
                    assert model.resolution == model.community == 1
                    resumed = observe(env, f"Web {theme} completed")
                    assert all(resumed["markers"].values())
                    detail = client.request("GET", f"/datasets/{env['kb']}")
                    assert detail["chunk_count"] == detail["document_count"] == 1
                    report.results.append(
                        {
                            "name": f"web.{theme}.pause_restart_readback",
                            "status": "passed",
                            "detail": {"pause_preserved_checkpoint": True, "new_task": new_task, "phases_not_repeated": True, "counts": [1, 1]},
                            "seconds": 0,
                        }
                    )
                    report.save()

    try:
        await asyncio.to_thread(browser)
        report.contact_sheets()
        report.finish()
        assert report.successful
    finally:
        client.session.close()
        report.save()
