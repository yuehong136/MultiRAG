"""Real PostgreSQL configuration readback and HTTP wire contracts (no model inference)."""

import base64
import json
import threading
from collections.abc import Iterator
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from typing import Any, cast

import pytest
import sqlalchemy as sa
from pypdf import PdfWriter
from sqlalchemy.orm import Session

from api.db.db_models import TenantModel, TenantModelInstance, TenantModelProvider
from api.db.services import provider_verification
from common import settings
from core.llm.ocr_model import PaddleOCROcrModel
from deepdoc.parser.paddleocr_parser import SUPPORTED_PADDLEOCR_ALGORITHMS, AlgorithmType
from tests.support.provider_instances import REAL_VERIFY_MODEL
from tests.support.provider_instances import http as http
from tests.support.provider_instances import owned_engine as owned_engine


@pytest.fixture
def ocr_wire_server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    calls: list[dict[str, Any]] = []
    jobs: dict[str, AlgorithmType] = {}
    polls: dict[str, int] = {}

    def result_for(algorithm: AlgorithmType) -> dict[str, Any]:
        if algorithm == "PP-OCRv5":
            return {"ocrResults": [{"prunedResult": {"rec_texts": ["First"], "rec_boxes": [[10, 20, 40, 80]]}}, {"prunedResult": {"rec_texts": ["Second"], "rec_boxes": [[18, 40, 80, 60]]}}]}
        return {
            "layoutParsingResults": [
                {"prunedResult": {"parsing_res_list": [{"block_content": "First", "block_label": "text", "block_bbox": [10, 20, 40, 80]}]}},
                {"prunedResult": {"parsing_res_list": [{"block_content": "Second", "block_label": "table", "block_bbox": [18, 40, 80, 60]}]}},
            ]
        }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            raw_body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/api/v2/ocr/jobs":
                multipart = BytesParser(policy=policy.default).parsebytes(f"Content-Type: {self.headers['Content-Type']}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw_body)
                parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True) for part in multipart.iter_parts()}
                algorithm = parts["model"].decode()
                assert algorithm in SUPPORTED_PADDLEOCR_ALGORITHMS
                calls.append({"path": self.path, "payload": json.loads(parts["optionalPayload"]), "file": parts["file"], "model": algorithm, "authorization": self.headers.get("Authorization")})
                job_id = f"wire-{len(jobs)}"
                jobs[job_id] = cast(AlgorithmType, algorithm)
                self.emit(json.dumps({"code": 0, "data": {"jobId": job_id}}).encode())
                return
            body = json.loads(raw_body)
            calls.append({"path": self.path, "payload": body, "authorization": self.headers.get("Authorization")})
            result = result_for("PP-OCRv5" if self.path == "/ocr" else "PaddleOCR-VL")
            encoded = json.dumps({"errorCode": 0, "result": result}).encode()
            self.emit(encoded)

        def do_GET(self) -> None:
            job_id = self.path.rsplit("/", 1)[-1]
            calls.append({"path": self.path, "authorization": self.headers.get("Authorization")})
            if self.path.startswith("/api/v2/ocr/jobs/"):
                polls[job_id] = polls.get(job_id, 0) + 1
                status = {"state": "pending"} if polls[job_id] == 1 else {"state": "done", "resultUrl": {"jsonUrl": f"http://127.0.0.1:{server.server_port}/results/{job_id}"}}
                self.emit(json.dumps({"code": 0, "data": status}).encode())
                return
            result = result_for(jobs[job_id])
            key, pages = next(iter(result.items()))
            encoded = "\n".join(json.dumps({"errorCode": 0, "result": {key: [page]}}) for page in pages).encode()
            self.emit(encoded)

        def emit(self, encoded: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("algorithm", SUPPORTED_PADDLEOCR_ALGORITHMS)
@pytest.mark.parametrize("protocol", ["sync", "job"])
def test_saved_model_roundtrip_and_pdf_http_contract(
    http: tuple[Any, ...], ocr_wire_server: tuple[str, list[dict[str, Any]]], algorithm: AlgorithmType, protocol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, owner, engine, _ = http
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "PaddleOCR", "llm": []}])
    monkeypatch.setattr(provider_verification, "verify_model", REAL_VERIFY_MODEL)
    base_url, calls = ocr_wire_server
    api_url = base_url + ("/api/v2/ocr/jobs" if protocol == "job" else "/ocr" if algorithm == "PP-OCRv5" else "/layout-parsing")
    config = {"paddleocr_api_url": api_url, "paddleocr_algorithm": algorithm, "paddleocr_access_token": "contract-test-token"}
    response = client.post(
        "/api/v1/providers/PaddleOCR/instances",
        json={"instance_name": "wire", "api_key": json.dumps(config), "model_ids": [], "custom_model": {"model_name": "wire-model", "model_types": ["ocr"], "max_tokens": 0}},
    )
    assert response.status_code == 200, response.text
    assert calls
    calls.clear()
    with Session(engine) as reader:
        row = reader.scalar(sa.select(TenantModel))
        assert row.model_types == ["ocr"] and row.max_tokens == 0
        assert reader.get(TenantModelProvider, row.provider_id).tenant_id == owner
        key = reader.get(TenantModelInstance, row.instance_id).api_key
        assert json.loads(key) == config
    model = PaddleOCROcrModel(key, "wire-model")
    pdf = PdfWriter()
    pdf.add_blank_page(width=200, height=300)
    pdf.add_blank_page(width=200, height=300)
    binary = BytesIO()
    pdf.write(binary)
    sections, tables = model.parse_pdf("wire.pdf", binary=binary.getvalue(), parse_method="pipeline", request_timeout=5)
    assert sections == [("First", "text", "@@1\t5.0\t20.0\t10.0\t40.0##"), ("Second", "text" if algorithm == "PP-OCRv5" else "table", "@@2\t9.0\t40.0\t20.0\t30.0##")]
    assert tables == []
    assert len(model.page_images) == 2
    cropped, positions = model.crop(sections[0][2], need_position=True)
    assert cropped is not None
    assert positions == [(0, 5, 20, 10, 40)]
    if protocol == "sync":
        assert len(calls) == 1
        assert calls[0]["path"] == ("/ocr" if algorithm == "PP-OCRv5" else "/layout-parsing")
        assert calls[0]["authorization"] == "token contract-test-token"
        assert base64.b64decode(calls[0]["payload"]["file"]) == binary.getvalue()
        assert calls[0]["payload"]["fileType"] == 0
    else:
        assert len(calls) == 4
        assert calls[0]["path"] == "/api/v2/ocr/jobs"
        assert calls[0]["model"] == algorithm
        assert calls[0]["file"] == binary.getvalue()
        assert all(call["authorization"] == "Bearer contract-test-token" for call in calls[:3])
        assert calls[-1]["path"].startswith("/results/")
        assert calls[-1]["authorization"] is None
    if algorithm == "PP-OCRv5":
        assert "formatBlockContent" not in calls[0]["payload"]
        assert "prettifyMarkdown" not in calls[0]["payload"]
    elif algorithm == "PP-StructureV3":
        assert "restructurePages" not in calls[0]["payload"]


@pytest.mark.parametrize(
    "config",
    [
        {"paddleocr_api_url": "http://127.0.0.1", "paddleocr_algorithm": "OCR"},
        {"paddleocr_api_url": "not-a-url", "paddleocr_algorithm": "PP-OCRv5"},
        {"paddleocr_api_url": "http://127.0.0.1/api/v2/ocr/jobs", "paddleocr_algorithm": "PP-OCRv5"},
    ],
)
def test_invalid_configuration_is_rejected_without_persistence(http: tuple[Any, ...], config: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, engine, _ = http
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "PaddleOCR", "llm": []}])
    monkeypatch.setattr(provider_verification, "verify_model", REAL_VERIFY_MODEL)
    response = client.post(
        "/api/v1/providers/PaddleOCR/instances",
        json={"instance_name": "wire", "api_key": json.dumps(config), "model_ids": [], "custom_model": {"model_name": "wire-model", "model_types": ["ocr"], "max_tokens": 0}},
    )
    assert response.status_code == 400
    with Session(engine) as reader:
        for table in (TenantModel, TenantModelInstance, TenantModelProvider):
            assert reader.scalar(sa.select(sa.func.count()).select_from(table)) == 0
