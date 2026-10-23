"""Real Web GraphRAG budget saves/reloads, SQL readback and worker consumption."""

import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import sqlalchemy as sa
from playwright.sync_api import expect, sync_playwright
from sqlalchemy.orm import Session, sessionmaker

from api.db.db_models import LLM, Knowledgebase, LLMFactories, TenantLLM
from common import settings
from core.graphrag.general import index
from scripts.acceptance.api import ProductAPI, response_data
from scripts.acceptance.browser import WebAcceptance
from scripts.acceptance.evidence import Evidence
from tests.support.dataset_management_http import management_api as management_api
from tests.support.graphrag_acceptance import DocumentExtractor, Model, api, worker
from tests.support.graphrag_acceptance import graphrag_api as graphrag_api
from tests.support.product_acceptance import product_web_server


async def test_web_batch_budget_save_reload_and_real_worker(graphrag_api: dict[str, Any], monkeypatch: Any) -> None:
    env = graphrag_api
    # Legacy model-list routes resolve JWT users through a synchronous alias;
    # bind both that alias and get_db's factory to the same real scratch SQL.
    from api import apps
    from api.db import db_models

    factory = sessionmaker(env["engine"], expire_on_commit=False)
    monkeypatch.setattr(apps, "SessionLocal", factory)
    monkeypatch.setattr(db_models, "SessionLocal", factory)
    model_name = "batch-settings-" + uuid4().hex
    with Session(env["engine"]) as db:
        created_factory = db.get(LLMFactories, "Builtin") is None
        if created_factory:
            db.add(LLMFactories(name="Builtin", tags="Text Embedding", status="1"))
        db.add(LLM(fid="Builtin", llm_name=model_name, mdl_type="embedding", max_tokens=8192, tags="Text Embedding", status="1"))
        registered = TenantLLM(tenant_id=env["tenant"], llm_factory="Builtin", llm_name=model_name, mdl_type="embedding", api_key="", max_tokens=8192)
        db.add(registered)
        db.flush()
        registered_id = registered.id
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["kb"]).values(embd_id=model_name + "@Builtin", tenant_embd_id=registered_id))
        db.commit()
    env["record"]["web_model"] = {"name": model_name, "tenant_model_id": registered_id, "factory_created": created_factory}
    doc_id = env["documents"][0]
    initial = env["reader"].query(env["collection"], filter=f'doc_id == "{doc_id}"', output_fields=["pk"], consistency_level="Strong")
    settings.docStoreConn.graph_delete_rows(env["collection"], env["kb"], [row["pk"] for row in initial])
    source = [" alpha" * 300, " beta" * 300]
    assert (
        settings.docStoreConn.insert(
            [
                {"id": uuid4().hex, "kb_id": env["kb"], "doc_id": doc_id, "page_num_int": page, "content_with_weight": content, "available_int": 1, "q_2_vec": [0.25, 0.75]}
                for page, content in enumerate(source)
            ],
            env["collection"],
            env["kb"],
        )
        == []
    )
    captured: list[list[str]] = []

    class RecordingExtractor(DocumentExtractor):
        async def __call__(self, doc: str, chunks: list[str], callback: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            captured.append(list(chunks))
            return await super().__call__(doc, chunks, callback, **kwargs)

    monkeypatch.setattr(index, "LightKGExt", RecordingExtractor)
    loop = asyncio.get_running_loop()
    report = Evidence(Path(env["graph_record_path"]).parent / ("batch-settings-web-" + env["kb"][:8]), mode="full")
    env["record"]["web_report"] = str(report.directory)
    report.metadata.update(
        limits=["Actual isolated Web/API/SQL/Redis/Milvus; deterministic controlled extraction/model, no paid provider or resident worker."],
        embedding="Controlled GraphRAG model and 2-dimensional embeddings; source batching uses the real token counter.",
        backend_base_sha="348e4f16fd63658e63693d7664d2d85fd2acacf2",
    )
    client = ProductAPI(env["base"], env["jwt"][0], env["kb"])
    report.secrets.append(client.token)

    def saved_budget() -> int:
        with env["engine"].connect() as db:
            config = db.scalar(sa.select(Knowledgebase.parser_config).where(Knowledgebase.id == env["kb"]))
        return config["graphrag"]["batch_chunk_token_size"]

    def browser() -> None:
        with product_web_server(env["base"], report) as base, sync_playwright() as playwright, playwright.chromium.launch(headless=True) as browser:
            view = WebAcceptance(client, report, base, lambda: None)
            for theme, locale, budget in [("light", "en-US", 512), ("dark", "zh-CN", 2048)]:
                errors: list[str] = []
                with view._context(browser, theme, (1440, 1000), errors) as context:
                    context.add_init_script("localStorage.setItem('i18n_language', " + repr(locale) + ");")
                    context.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(status=200, content_type="text/css", body=""))
                    page = context.new_page()
                    view._observe(page, errors, [])
                    view._navigate(page, f"/knowledge/{env['kb']}/settings")
                    label = "Extraction batch tokens" if locale == "en-US" else "抽取批次 Token 数"
                    method_label = "Method" if locale == "en-US" else "方法"
                    item = page.locator("label").filter(has_text=label).locator("..")
                    number = item.locator('input[type="number"]')
                    method = page.locator("label").filter(has_text=re.compile("^" + re.escape(method_label))).locator("..").get_by_role("combobox")
                    expect(number).to_be_visible()
                    if number.is_disabled():
                        method.click()
                        page.get_by_role("option", name="Light", exact=True).click()
                    number.fill(str(budget))
                    number.press("Tab")
                    with page.expect_response(lambda reply: reply.request.method == "PUT" and urlsplit(reply.url).path == f"/api/v1/datasets/{env['kb']}") as saved:
                        submit = page.locator('button[type="submit"][form="kb-settings-form"]')
                        submit.focus()
                        page.keyboard.press("Enter")
                    saved_body = saved.value.json()
                    report.metadata.setdefault("save_responses", []).append(
                        {
                            "http": saved.value.status,
                            "code": saved_body.get("code"),
                            "message": saved_body.get("message"),
                            "graphrag": (saved_body.get("data") or {}).get("parser_config", {}).get("graphrag"),
                        }
                    )
                    report.save()
                    assert response_data(saved_body, label="Web GraphRAG budget save", http_status=saved.value.status)["parser_config"]["graphrag"]["batch_chunk_token_size"] == budget
                    assert saved_budget() == budget
                    assert client.request("GET", f"/datasets/{env['kb']}")["parser_config"]["graphrag"]["batch_chunk_token_size"] == budget
                    page.reload(wait_until="networkidle")
                    expect(number).to_have_value(str(budget))
                    number.scroll_into_view_if_needed()
                    view._screenshot(page, "batch-budget-reloaded", theme, (1440, 1000), status=locale + " saved and reloaded")
                    if locale == "en-US":
                        task_id = api(env, "POST")["data"]["task_id"]
                        asyncio.run_coroutine_threadsafe(worker(env, task_id, Model()), loop).result(timeout=60)
                        assert len(captured) == 1 and len(captured[0]) == 2 and captured[0] == source
                    method.click()
                    page.get_by_role("option", name="NER", exact=True).click()
                    expect(number).to_be_disabled()
                    expect(number).to_have_value(str(budget))
                    with page.expect_response(lambda reply: reply.request.method == "PUT" and urlsplit(reply.url).path == f"/api/v1/datasets/{env['kb']}") as ner_saved:
                        page.locator('button[type="submit"][form="kb-settings-form"]').click()
                    response_data(ner_saved.value.json(), label="Web NER preserves budget", http_status=ner_saved.value.status)
                    page.reload(wait_until="networkidle")
                    expect(number).to_be_disabled()
                    expect(number).to_have_value(str(budget))
                    assert saved_budget() == budget
                    number.scroll_into_view_if_needed()
                    view._screenshot(page, "ner-budget-preserved", theme, (1440, 1000), status=locale + " NER value preserved")
                    assert errors == [], errors
                    report.results.append(
                        {"name": f"web.{theme}.{locale}.graph_batch_budget", "status": "passed", "detail": {"saved": budget, "reload": True, "ner_preserved": True, "sql_readback": True}, "seconds": 0}
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
        with env["engine"].begin() as db:
            db.execute(sa.delete(TenantLLM).where(TenantLLM.id == registered_id))
            db.execute(sa.delete(LLM).where(LLM.fid == "Builtin", LLM.llm_name == model_name))
            if created_factory:
                db.execute(sa.delete(LLMFactories).where(LLMFactories.name == "Builtin"))
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(TenantLLM).where(TenantLLM.id == registered_id)) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(LLM).where(LLM.fid == "Builtin", LLM.llm_name == model_name)) == 0
        env["record"]["web_model"]["cleanup_absent"] = True
