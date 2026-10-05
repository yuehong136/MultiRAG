"""Real Web actions and light/dark evidence; visual approval remains human."""

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal
from urllib.parse import urlsplit

from playwright.sync_api import Browser, BrowserContext, Page, Route, expect, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from scripts.acceptance.api import MARKER, SOURCE, ProductAPI, response_data
from scripts.acceptance.evidence import Evidence, require

VIEWPORTS = ((1440, 1000), (1024, 768))
API_PATTERN = re.compile(r"^https?://[^/]+/(?:api/)?v1/")


class WebAcceptance:
    def __init__(self, api: ProductAPI, evidence: Evidence, web_base: str, run_worker: Callable[[], None], *, headed: bool = False) -> None:
        self.api = api
        self.evidence = evidence
        self.web_base = web_base.rstrip("/")
        self.run_worker = run_worker
        self.headed = headed
        self.user = api.request("GET", "/users/me")

    @contextmanager
    def _context(self, browser: Browser, theme: Literal["light", "dark"], viewport: tuple[int, int], errors: list[str]) -> Iterator[BrowserContext]:
        context = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]}, color_scheme=theme, locale="en-US")
        context.set_default_timeout(15000)
        token = self.api.token
        storage = {
            "auth_token": token,
            "user_info": json.dumps(self.user),
            "auth-storage": json.dumps({"state": {"user": self.user, "tenant": None, "token": token, "isAuthenticated": True}, "version": 0}),
            "theme": theme,
            "language": "en",
            "i18nextLng": "en",
            "ui-storage": json.dumps({"state": {"theme": theme, "language": "en"}, "version": 1}),
        }
        origin = urlsplit(self.web_base)
        web_origin = f"{origin.scheme}://{origin.netloc}"
        context.add_init_script(
            "(() => { const origin = "
            + json.dumps(web_origin)
            + "; const storage = "
            + json.dumps(storage)
            + "; if (location.origin === origin) for (const [key, value] of Object.entries(storage)) localStorage.setItem(key, value); })();"
        )

        def relay(route: Route) -> None:
            # Preserve Web method, body and auth; only point transport at our
            # owned API. Never synthesize a business-success response.
            requested = urlsplit(route.request.url)
            target = self.api.base + requested.path + ("?" + requested.query if requested.query else "")
            try:
                reply = route.fetch(url=target, max_redirects=0, timeout=30000)
                if reply.status >= 400:
                    errors.append(f"HTTP {reply.status}: {requested.path}")
                elif "json" in reply.headers.get("content-type", ""):
                    try:
                        body = reply.json()
                        codes = [body[key] for key in ("code", "retcode") if key in body] if isinstance(body, dict) else []
                        if codes and any(type(code) is not int or code != 0 for code in codes):
                            errors.append(f"Business error {codes}: {requested.path}")
                    except ValueError:
                        errors.append(f"Invalid JSON: {requested.path}")
                route.fulfill(response=reply)
            except PlaywrightError as exc:
                errors.append(f"API relay {type(exc).__name__}: {requested.path}")
                try:
                    route.abort()
                except PlaywrightError:
                    errors.append(f"API route could not be aborted: {requested.path}")

        context.route(API_PATTERN, relay)
        try:
            yield context
        finally:
            try:
                # Polling requests can arrive while the finite worker runs.
                # Drain owned handlers before closing this browser context.
                context.unroute_all(behavior="wait")
                require(not errors, "; ".join(errors))
            finally:
                context.close()

    @staticmethod
    def _observe(page: Page, errors: list[str], cancelled: list[str]) -> None:
        page.on("pageerror", lambda error: errors.append(f"JavaScript: {error.message}"))
        # React Query cancels obsolete fetches during remount/navigation.
        # Record those separately; the active operation still needs a real
        # success response plus its content/readback assertions below.
        page.on("requestfailed", lambda request: (cancelled if request.failure == "net::ERR_ABORTED" else errors).append(f"{request.failure}: {urlsplit(request.url).path}"))

    def _navigate(self, page: Page, route: str) -> None:
        page.goto(self.web_base + route, wait_until="networkidle", timeout=45000)
        require(urlsplit(page.url).path == route, f"Unexpected navigation from {route} to {urlsplit(page.url).path}")
        expect(page.locator("#root")).to_be_visible()
        page.evaluate("document.fonts.ready")

    def _screenshot(self, page: Page, slug: str, theme: str, viewport: tuple[int, int], *, status: str) -> None:
        filename = f"{slug}-{theme}-{viewport[0]}x{viewport[1]}.png"
        page.screenshot(path=str(self.evidence.directory / filename), animations="disabled", full_page=True)
        self.evidence.screenshots.append({"file": filename, "route": urlsplit(page.url).path, "theme": theme, "viewport": f"{viewport[0]}x{viewport[1]}", "status": status})
        self.evidence.save()

    def _settings(self, page: Page) -> dict[str, Any]:
        self._navigate(page, f"/knowledge/{self.api.dataset}/settings")
        description = page.locator('textarea[name="description"]')
        value = "Repeatable Web configuration readback"
        expect(description).to_be_visible()
        description.fill(value)
        chunk_size = page.locator("label").filter(has_text="Recommended chunk size").locator("..").locator('input[type="number"]')
        expect(chunk_size).to_be_visible()
        chunk_size.fill("321")
        with page.expect_response(lambda reply: urlsplit(reply.url).path == "/api/v1" + self.api.path and reply.request.method == "PUT") as captured:
            page.locator('button[type="submit"][form="kb-settings-form"]').click()
        reply = captured.value
        response_data(reply.json(), label="Web configuration save", http_status=reply.status)
        saved = self.api.request("GET", self.api.path)
        require(saved["description"] == value, "Web description independent GET mismatch")
        require(saved["parser_config"]["chunk_token_num"] == 321, "Web chunk size independent GET mismatch")
        page.reload(wait_until="networkidle")
        expect(page.locator('textarea[name="description"]')).to_have_value(value)
        expect(chunk_size).to_have_value("321")
        return {"description_readback": True, "chunk_token_num": 321, "reload_persists": True}

    def _upload_parse(self, page: Page) -> str:
        self._navigate(page, f"/knowledge/{self.api.dataset}/documents")
        page.get_by_role("button", name="Add document", exact=True).click()
        page.get_by_role("button", name="Upload files", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        dialog.locator('input[type="file"]').first.set_input_files({"name": "acceptance-web.txt", "mimeType": "text/plain", "buffer": SOURCE})
        # Make upload and parse individually observable. The switch defaults
        # to parsing on upload in some versions of the Web client.
        switch = dialog.get_by_role("switch")
        if switch.get_attribute("aria-checked") == "true":
            switch.click()
        with page.expect_response(lambda reply: urlsplit(reply.url).path == "/api/v1" + self.api.path + "/documents" and reply.request.method == "POST") as captured:
            dialog.get_by_role("button", name=re.compile(r"^Upload.*1", re.I)).click()
        reply = captured.value
        docs = response_data(reply.json(), label="Web upload", http_status=reply.status)
        require(isinstance(docs, list) and len(docs) == 1, "Web upload expected one document")
        identifier = docs[0]["id"]
        self.api.verify_upload(identifier, "acceptance-web.txt")
        if dialog.is_visible():
            dialog.get_by_role("button", name=re.compile(r"Cancel|Close", re.I)).first.click()
        row = page.get_by_role("row").filter(has_text="acceptance-web.txt")
        expect(row).to_be_visible()
        with page.expect_response(lambda reply: urlsplit(reply.url).path == "/api/v1" + self.api.path + "/documents/parse" and reply.request.method == "POST") as captured_parse:
            row.get_by_role("button", name=re.compile(r"Start pars", re.I)).click()
        reply = captured_parse.value
        response_data(reply.json(), label="Web parse start", http_status=reply.status)
        self.run_worker()
        self.api.wait_parsed(identifier)
        self.api.chunks(identifier)
        page.reload(wait_until="networkidle")
        expect(page.get_by_role("row").filter(has_text="acceptance-web.txt")).to_be_visible()
        return identifier

    def _search(self, page: Page, identifier: str) -> dict[str, Any]:
        query = page.locator("#root textarea").first
        expect(query).to_be_visible()
        query.fill("orchid approval code")
        with page.expect_response(lambda reply: urlsplit(reply.url).path == "/api/v1" + self.api.path + "/search" and reply.request.method == "POST") as captured:
            # Also exercise the documented keyboard submit path.
            query.press("Enter")
        reply = captured.value
        data = response_data(reply.json(), label="Web retrieval", http_status=reply.status)
        self.api.verify_retrieval(data, identifier)
        card = page.locator("article").filter(has_text="acceptance-api.txt").first
        expect(card).to_be_visible()
        expect(card).to_contain_text("17 days")
        card.get_by_role("button", name="Details", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name=re.compile(r"^(Raw|Preview)$")).click()
        expect(dialog.locator("pre")).to_contain_text(MARKER)
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        return {"document_id": identifier, "total": data["total"], "keyboard_submit": True, "source_preview_matches": True}

    def run(self, identifier: str) -> None:
        with sync_playwright() as playwright:
            with playwright.chromium.launch(headless=not self.headed) as browser:
                errors: list[str] = []
                cancelled: list[str] = []
                with self._context(browser, "light", VIEWPORTS[0], errors) as context:
                    page = context.new_page()
                    self._observe(page, errors, cancelled)
                    self.evidence.check("web.configuration.save_readback_reload", lambda: self._settings(page))
                    web_documents: list[str] = []

                    def upload_parse() -> dict[str, Any]:
                        web_documents.append(self._upload_parse(page))
                        return {"document_id": web_documents[0], "upload_readback": True, "worker_completed": True, "chunks_nonempty": True}

                    self.evidence.check("web.upload_parse_chunks", upload_parse)

                    def workflow_errors() -> dict[str, Any]:
                        require(not errors, "; ".join(errors))
                        return {"errors": [], "cancelled_obsolete_requests": cancelled.copy()}

                    self.evidence.check("web.workflow.errors", workflow_errors)
                    self._screenshot(page, "workflow", "light", VIEWPORTS[0], status="evidence")
                views = [
                    ("datasets", "/knowledge"),
                    ("documents", f"/knowledge/{self.api.dataset}/documents"),
                    ("chunks", f"/knowledge/{self.api.dataset}/documents/{identifier}/chunks"),
                    ("retrieval", f"/knowledge/{self.api.dataset}/search"),
                    ("configuration", f"/knowledge/{self.api.dataset}/settings"),
                    ("profile", "/settings/profile"),
                    ("api-keys", "/settings/api-keys"),
                ]
                for viewport in VIEWPORTS:
                    for theme in ("light", "dark"):
                        errors = []
                        cancelled = []
                        with self._context(browser, theme, viewport, errors) as context:
                            page = context.new_page()
                            self._observe(page, errors, cancelled)
                            for slug, route in views:
                                errors.clear()
                                cancelled.clear()

                                def inspect(route: str = route, slug: str = slug) -> dict[str, Any]:
                                    self._navigate(page, route)
                                    expect(page.locator("html")).to_have_attribute("data-theme", theme)
                                    text = page.locator("#root").inner_text()
                                    require(len(text.strip()) > 30, f"{slug}: blank content")
                                    require(not re.search(r"Something went wrong|Page not found|Failed to load", text, re.I), f"{slug}: error/empty fallback")
                                    if slug == "retrieval":
                                        self._search(page, identifier)
                                    if slug == "chunks":
                                        expect(page.locator("#root").get_by_text(re.compile(MARKER)).first).to_be_visible()
                                    if slug == "documents":
                                        expect(page.get_by_text("acceptance-api.txt", exact=True).first).to_be_visible()
                                    if slug == "configuration":
                                        expect(page.locator('textarea[name="description"]')).to_be_visible()
                                    if slug == "profile":
                                        expect(page.get_by_role("heading", name="Account details", exact=True)).to_be_visible()
                                        expect(page.get_by_text(self.user["email"], exact=True)).to_be_visible()
                                    if slug == "api-keys":
                                        expect(page.get_by_role("heading", name="API documentation", exact=True)).to_be_visible()
                                        spec = context.request.get(self.web_base + "/openapi.json")
                                        require(spec.status == 200 and bool(spec.json().get("paths")), "Web API documentation source is missing/empty")
                                    page.keyboard.press("Tab")
                                    require(page.evaluate("document.activeElement !== document.body"), f"{slug}: no keyboard focus target")
                                    overflow = page.evaluate("Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - innerWidth")
                                    require(overflow <= 1, f"{slug}: horizontal page overflow {overflow}px")
                                    require(not errors, "; ".join(errors))
                                    return {"theme_applied": theme, "page_overflow_px": overflow, "keyboard_focus": True, "cancelled_obsolete_requests": cancelled.copy()}

                                passed = self.evidence.check(f"web.{slug}.{theme}.{viewport[0]}x{viewport[1]}", inspect)
                                self._screenshot(page, slug, theme, viewport, status="passed" if passed else "failed")
                self.evidence.contact_sheets()
