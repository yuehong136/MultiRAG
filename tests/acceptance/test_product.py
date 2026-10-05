"""Explicit product acceptance, including real Web consumers and retained views."""

import os
from typing import Any

from scripts.acceptance.api import ProductAPI
from scripts.acceptance.browser import WebAcceptance
from scripts.acceptance.evidence import Evidence, require
from tests.support.product_acceptance import finite_worker, product_web_server
from tests.support.product_acceptance import parse_api as parse_api
from tests.support.product_acceptance import product_evidence as product_evidence
from tests.support.product_acceptance import product_runtime as product_runtime
from tests.support.product_acceptance import runtime_upload_api as runtime_upload_api


def test_repeatable_product_checks(product_evidence: Evidence, product_runtime: dict[str, Any]) -> None:
    report, env = product_evidence, product_runtime
    api = ProductAPI(env["base"], env["jwt"], env["kb"])
    worker = finite_worker(env, report)
    documents: list[str] = []
    try:
        configured = report.check("api.configuration.save_readback_omission_clear", api.configuration)
        if not configured:
            report.blocked("api.ingestion", "Configuration prerequisite failed")
            return

        def upload() -> dict[str, Any]:
            documents.append(api.upload())
            return api.verify_upload(documents[0], "acceptance-api.txt")

        if not report.check("api.upload.readback", upload):
            report.blocked("api.parse_chunks_retrieval", "Upload prerequisite failed")
            return
        identifier = documents[0]

        def parse() -> dict[str, Any]:
            api.request("POST", api.path + "/documents/parse", json={"document_ids": [identifier]})
            worker()
            return api.wait_parsed(identifier)

        parsed = report.check("api.parse.completed_nonzero_chunks", parse)
        if parsed:
            report.check("api.chunks.source_content", lambda: api.chunks(identifier))
            report.check("api.retrieval.uploaded_document_content", lambda: api.retrieval(identifier))
        else:
            report.blocked("api.chunks_retrieval", "Parse prerequisite failed")
        if report.metadata["mode"] == "full":

            def web() -> None:
                with product_web_server(env["base"], report) as base:
                    WebAcceptance(api, report, base, worker, headed=os.environ.get("MULTIRAG_ACCEPTANCE_HEADED") == "1").run(identifier)

            report.check("web.execution", web)
    finally:
        api.session.close()
        report.save()
    require(report.successful, f"Product acceptance failed; inspect {report.directory / 'acceptance.md'}")
