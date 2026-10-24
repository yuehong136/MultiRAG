"""Real PDF/image/ZIP and parser consumers; only tenant DB and HTTP peers are controlled."""

import base64
import json
import threading
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from api.db.joint_services import tenant_model_service
from api.db.services.tenant_llm_service import TenantLangfuseService, TenantLLMService
from common.constants import LLMType
from core.app import naive
from core.llm.cv import OpenAI_APICV
from deepdoc.parser import mineru_parser
from deepdoc.parser.mineru_parser import MinerUParser
from tests.pdf_bbox_support import make_pdf


def image_block(path: str = "images/figure.png", **fields: Any) -> dict[str, Any]:
    return {"type": "image", "img_path": path, "image_caption": ["Figure caption"], "image_footnote": ["Figure footnote"], "page_idx": 0, "bbox": [100, 100, 500, 500], **fields}


def image_bytes(size: tuple[int, int] = (32, 32), mode: str = "RGB") -> bytes:
    with BytesIO() as stream, Image.new(mode, size) as image:
        image.save(stream, format="PNG")
        return stream.getvalue()


@pytest.fixture(autouse=True)
def owned_parser_tempdirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep all parser scratch under the registered pytest root and read back cleanup."""
    original = mineru_parser.tempfile.mkdtemp
    paths: list[Path] = []

    def allocate(suffix: str = "", prefix: str = "tmp", dir: Any = None) -> str:
        path = original(suffix=suffix, prefix=prefix, dir=dir or tmp_path)
        paths.append(Path(path))
        return path

    monkeypatch.setattr(mineru_parser.tempfile, "mkdtemp", allocate)
    yield
    assert all(not path.exists() for path in paths), paths


@pytest.mark.parametrize("parse_method", ["raw", "manual", "pipeline", "paper"])
@pytest.mark.parametrize("caption", [True, False])
def test_description_preserves_captions_positions_and_section_shape(parse_method: str, caption: bool) -> None:
    parser = MinerUParser()
    block = image_block(vlm_description="  Visible growth from 12 to 34.  ")
    if not caption:
        block.update(image_caption=[], image_footnote=[])
    baseline = parser._transfer_to_sections([block | {"vlm_description": ""}], parse_method)[0]
    actual = parser._transfer_to_sections([block], parse_method)[0]
    expected = "Figure caption\nFigure footnote\nVisible growth from 12 to 34." if caption else "Visible growth from 12 to 34."
    assert actual[0] == expected + (parser._line_tag(block) if parse_method == "paper" else "")
    assert actual[1:] == baseline[1:]
    assert len(actual) == len(baseline)


@pytest.mark.parametrize("description", [None, "", " \n "])
def test_empty_description_leaves_caption_fallback_unchanged(description: str | None) -> None:
    parser = MinerUParser()
    baseline = parser._transfer_to_sections([image_block()], "raw")
    assert parser._transfer_to_sections([image_block(vlm_description=description)], "raw") == baseline


def test_enrichment_skips_invalid_images_and_keeps_per_image_failures_optional(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    good = tmp_path / "good.png"
    rgba = tmp_path / "rgba.png"
    tiny = tmp_path / "tiny.png"
    corrupt = tmp_path / "corrupt.png"
    for path, data in [(good, image_bytes()), (rgba, image_bytes(mode="RGBA")), (tiny, image_bytes((8, 8))), (corrupt, b"not an image")]:
        path.write_bytes(data)
    seen: list[tuple[bytes, str]] = []

    class Vision:
        def describe_with_prompt(self, binary: bytes, prompt: str) -> str:
            seen.append((binary, prompt))
            if binary.startswith(b"\x89PNG"):
                raise RuntimeError("controlled vision failure")
            return "```markdown\nVisible figure semantics.\n```"

    outputs = [image_block(str(path)) for path in (good, rgba, tiny, corrupt, tmp_path / "missing.png")]
    outputs.extend([image_block(""), {"type": "text", "text": "Text block", "img_path": str(good)}])
    MinerUParser()._enhance_images_with_vlm(outputs, Vision(), language="Chinese")
    assert outputs[0]["vlm_description"] == "Visible figure semantics."
    assert all("vlm_description" not in item for item in outputs[1:])
    assert len(seen) == 2
    assert all("Chinese" in prompt for _, prompt in seen)
    assert "controlled vision failure" in caplog.text


def test_enrichment_bounds_submitted_jobs_and_closes_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image = tmp_path / "figure.png"
    image.write_bytes(image_bytes())
    state = {"peak": 0}
    futures: list[Any] = []
    opened: list[Image.Image] = []
    real_open = mineru_parser.Image.open

    def tracked_open(*args: Any, **kwargs: Any) -> Image.Image:
        result = real_open(*args, **kwargs)
        opened.append(result)
        return result

    class TrackedExecutor(ThreadPoolExecutor):
        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
            future = super().submit(fn, *args, **kwargs)
            futures.append(future)
            state["peak"] = max(state["peak"], sum(not item.done() for item in futures))
            return future

    class Vision:
        def describe_with_prompt(self, binary: bytes, prompt: str) -> str:
            threading.Event().wait(0.02)  # Controlled latency exposes eager job submission.
            return "Visible semantics."

    monkeypatch.setattr(mineru_parser.Image, "open", tracked_open)
    monkeypatch.setattr("concurrent.futures.ThreadPoolExecutor", TrackedExecutor)
    outputs = [image_block(str(image)) for _ in range(19)]
    MinerUParser()._enhance_images_with_vlm(outputs, Vision())
    assert all(item["vlm_description"] == "Visible semantics." for item in outputs)
    assert 1 <= state["peak"] <= 4
    assert len(opened) == len(outputs)
    for item in opened:
        with pytest.raises(ValueError, match="closed image"):
            item.getpixel((0, 0))


@pytest.fixture
def mineru_http() -> Iterator[Any]:
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("report/vlm/report_content_list.json", json.dumps([image_block(), {"type": "text", "text": "Visible paragraph.", "page_idx": 0, "bbox": [100, 600, 700, 700]}]))
        zipped.writestr("report/vlm/images/figure.png", image_bytes())
    state = SimpleNamespace(requests=[], status=200, description="Visible revenue rises from 12 to 34.", archive=archive.getvalue())

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state.requests.append((self.path, body))
            if self.path == "/file_parse":
                response = state.archive
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
            elif self.path == "/v1/chat/completions":
                response = json.dumps(
                    {
                        "id": "controlled-vision",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "tenant-vision",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": state.description}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
                    }
                ).encode()
                self.send_response(state.status)
                self.send_header("Content-Type", "application/json")
            else:
                response = b"unknown controlled endpoint"
                self.send_response(404)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.fixture
def tenant_models(monkeypatch: pytest.MonkeyPatch, db: Session, mineru_http: Any) -> Iterator[Any]:
    state = SimpleNamespace(open=False, defaults=[], bundles=[], usages=[], missing=False)
    parser = MinerUParser(mineru_api=mineru_http.url)
    vision = OpenAI_APICV("controlled-test-key", "tenant-vision", base_url=mineru_http.url)

    @contextmanager
    def connection() -> Iterator[Session]:
        state.open = True
        try:
            yield db
        finally:
            state.open = False

    def ocr_config(session: Session, tenant_id: str, model_type: str, name: str) -> dict[str, Any]:
        assert session is db and tenant_id == "tenant-f" and model_type == LLMType.OCR.value and name == "mineru@MinerU"
        return {"id": 10, "tenant_id": tenant_id, "llm_name": name, "mdl_type": model_type, "llm_factory": "MinerU"}

    def default_config(session: Session, tenant_id: str, model_type: Any) -> dict[str, Any]:
        assert session is db and tenant_id == "tenant-f" and model_type == LLMType.IMAGE2TEXT
        state.defaults.append(tenant_id)
        if state.missing:
            raise LookupError("No tenant IMAGE2TEXT model")
        return {"id": 20, "tenant_id": tenant_id, "llm_name": "tenant-vision", "mdl_type": LLMType.IMAGE2TEXT.value, "llm_factory": "OpenAI-API-Compatible"}

    def model_instance(session: Session, tenant_id: str, config: dict[str, Any], lang: str, **kwargs: Any) -> Any:
        assert state.open and session is db and tenant_id == config["tenant_id"] == "tenant-f"
        state.bundles.append((config["id"], lang))
        return parser if config["id"] == 10 else vision

    def usage(model_id: int, tokens: int) -> bool:
        assert not state.open
        state.usages.append((model_id, tokens))
        return True

    monkeypatch.setattr(naive, "db_connection", connection)
    monkeypatch.setattr(naive, "get_model_config_by_type_and_name", ocr_config)
    monkeypatch.setattr(naive, "get_tenant_default_model_by_type", default_config)
    monkeypatch.setattr(TenantLLMService, "model_instance", staticmethod(model_instance))
    monkeypatch.setattr(TenantLLMService, "increase_usage_by_id", staticmethod(usage))
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", staticmethod(lambda *args, **kwargs: None))
    state.parser = parser
    state.vision = vision
    try:
        yield state
    finally:
        vision.client.close()


@pytest.mark.parametrize("language", ["Chinese", "English", "French"])
def test_naive_chunk_real_pdf_zip_vlm_http_and_searchable_positions(tenant_models: Any, mineru_http: Any, language: str, tmp_path: Path) -> None:
    progress: list[tuple[Any, Any]] = []
    chunks = naive.chunk(
        "report.pdf",
        binary=make_pdf(1),
        lang=language,
        callback=lambda prog, msg: progress.append((prog, msg)),
        tenant_id="tenant-f",
        backend="vlm-http-client",
        parser_config={"layout_recognize": "mineru@MinerU@MinerU", "chunk_token_num": 0},
    )
    assert chunks
    figure = next(chunk for chunk in chunks if "Figure caption" in chunk["content_with_weight"])
    assert "Figure footnote" in figure["content_with_weight"]
    assert mineru_http.description in figure["content_with_weight"]
    assert naive.rag_tokenizer.tokenize("revenue") in figure["content_ltks"]
    assert figure["page_num_int"] == [1] and figure["position_int"]
    assert "@@" not in figure["content_with_weight"]
    assert tenant_models.defaults == ["tenant-f"]
    assert tenant_models.bundles == [(10, language), (20, language)]
    assert tenant_models.usages == [(20, 7)]
    assert [path for path, _ in mineru_http.requests] == ["/file_parse", "/v1/chat/completions"]
    pdf_request = mineru_http.requests[0][1]
    assert b"%PDF-" in pdf_request and b'filename="report.pdf"' in pdf_request
    assert b"vlm-http-client" in pdf_request
    vision_request = json.loads(mineru_http.requests[1][1])
    content = vision_request["messages"][0]["content"]
    assert any(language in part.get("text", "") for part in content)
    image_url = next(part["image_url"]["url"] for part in content if part["type"] == "image_url")
    with Image.open(BytesIO(base64.b64decode(image_url.split(",", 1)[1]))) as uploaded:
        assert uploaded.size == (32, 32)
    assert any(prog == 0.78 for prog, _ in progress)


@pytest.mark.parametrize("scenario", ["missing", "disabled", "provider-failure", "empty"])
def test_optional_vision_failures_preserve_actual_parser_results(tenant_models: Any, mineru_http: Any, scenario: str) -> None:
    tenant_models.missing = scenario == "missing"
    mineru_http.status = 400 if scenario == "provider-failure" else 200
    mineru_http.description = "  " if scenario == "empty" else mineru_http.description
    options = {"vision_model": None} if scenario == "disabled" else {}
    sections, tables, parser = naive.by_mineru("report.pdf", binary=make_pdf(1), tenant_id="tenant-f", mineru_llm_name="mineru@MinerU", lang="Chinese", **options)
    assert parser is tenant_models.parser
    assert tables == []
    assert sections[0][0] == "Figure caption\nFigure footnote"
    assert sections[1][0] == "Visible paragraph."
    assert tenant_models.defaults == ([] if scenario == "disabled" else ["tenant-f"])
    assert len(mineru_http.requests) == (1 if scenario in {"missing", "disabled"} else 2)
    assert tenant_models.usages == ([(20, 7)] if scenario == "empty" else [])


@pytest.mark.parametrize("language", [None, ""])
def test_missing_language_uses_english(tenant_models: Any, mineru_http: Any, language: Any) -> None:
    class Vision:
        def describe_with_prompt(self, binary: bytes, prompt: str) -> str:
            assert "English" in prompt
            return "English figure description."

    sections, _ = tenant_models.parser.parse_pdf("report.pdf", binary=make_pdf(1), lang=language, vision_model=Vision())
    assert "English figure description." in sections[0][0]


def test_caller_vision_model_is_used_without_default_lookup(tenant_models: Any, mineru_http: Any) -> None:
    class Vision:
        def describe_with_prompt(self, binary: bytes, prompt: str) -> str:
            return "Explicit figure description."

    sections, _, parser = naive.by_mineru("report.pdf", binary=make_pdf(1), tenant_id="tenant-f", mineru_llm_name="mineru@MinerU", vision_model=Vision())
    assert parser is tenant_models.parser
    assert "Explicit figure description." in sections[0][0]
    assert tenant_models.defaults == [] and len(mineru_http.requests) == 1


def test_enrichment_setup_failure_preserves_parser_results(tenant_models: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("controlled enhancement setup failure")

    monkeypatch.setattr(tenant_models.parser, "_enhance_images_with_vlm", fail)
    sections, tables, parser = naive.by_mineru("report.pdf", binary=make_pdf(1), tenant_id="tenant-f", mineru_llm_name="mineru@MinerU")
    assert parser is tenant_models.parser and tables == []
    assert sections[0][0] == "Figure caption\nFigure footnote"


def test_mineru_language_override_reaches_figure_prompt(tenant_models: Any, mineru_http: Any) -> None:
    naive.by_mineru("report.pdf", binary=make_pdf(1), tenant_id="tenant-f", mineru_llm_name="mineru@MinerU", lang="English", parser_config={"mineru_lang": "French"})
    request = json.loads(mineru_http.requests[-1][1])
    assert any("French" in part.get("text", "") for part in request["messages"][0]["content"])


def test_no_tenant_does_not_resolve_models(tenant_models: Any) -> None:
    assert naive.by_mineru("report.pdf", binary=make_pdf(1)) == (None, None, None)
    assert tenant_models.defaults == [] and tenant_models.bundles == []


def test_foreign_tenant_default_is_denied_before_vision_construction(tenant_models: Any, mineru_http: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    queries: list[dict[str, Any]] = []

    def tenant(session: Session, **filters: Any) -> Any:
        assert filters == {"id": "tenant-f"}
        return SimpleNamespace(img2txt_id="foreign@Other", tenant_img2txt_id=20)

    def model(session: Session, **filters: Any) -> Any:
        queries.append(filters)
        # This ID belongs to another tenant. The active tenant predicate must
        # prevent even loading its credentials, and there is no global fallback.
        assert filters == {"id": 20, "tenant_id": "tenant-f"}
        return None

    monkeypatch.setattr(naive, "get_tenant_default_model_by_type", tenant_model_service.get_tenant_default_model_by_type)
    monkeypatch.setattr(tenant_model_service.TenantService, "get_or_none", staticmethod(tenant))
    monkeypatch.setattr(TenantLLMService, "get_or_none", staticmethod(model))
    sections, _, parser = naive.by_mineru("report.pdf", binary=make_pdf(1), tenant_id="tenant-f", mineru_llm_name="mineru@MinerU")
    assert parser is tenant_models.parser and sections[0][0] == "Figure caption\nFigure footnote"
    assert queries == [{"id": 20, "tenant_id": "tenant-f"}]
    assert tenant_models.bundles == [(10, "Chinese")]
    assert [path for path, _ in mineru_http.requests] == ["/file_parse"]


def test_without_readable_images_does_not_invoke_vision() -> None:
    class Vision:
        def describe_with_prompt(self, binary: bytes, prompt: str) -> str:
            pytest.fail("No readable images must not spend a vision request")

    output = [{"type": "text", "text": "Text only."}, image_block("")]
    MinerUParser()._enhance_images_with_vlm(output, Vision())
    assert all("vlm_description" not in item for item in output)


def test_mineru_output_cannot_upload_images_from_other_directories(tenant_models: Any, mineru_http: Any, tmp_path: Path) -> None:
    outside = tmp_path / "other-tenant.png"
    outside.write_bytes(image_bytes())
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("report/auto/report_content_list.json", json.dumps([image_block(str(outside))]))
    mineru_http.archive = archive.getvalue()
    sections, _, parser = naive.by_mineru("report.pdf", binary=make_pdf(1), tenant_id="tenant-f", mineru_llm_name="mineru@MinerU")
    assert parser is tenant_models.parser and sections[0][0] == "Figure caption\nFigure footnote"
    assert [path for path, _ in mineru_http.requests] == ["/file_parse"]
    assert tenant_models.usages == []


def test_symlink_outside_output_is_not_sent_to_vision(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    outside = tmp_path / "other-tenant.png"
    outside.write_bytes(image_bytes())
    link = output_dir / "figure.png"
    link.symlink_to(outside)

    class Vision:
        def describe_with_prompt(self, binary: bytes, prompt: str) -> str:
            pytest.fail("An output symlink must not upload another directory's image")

    outputs = [image_block(str(link))]
    MinerUParser()._enhance_images_with_vlm(outputs, Vision(), output_dir=output_dir)
    assert "vlm_description" not in outputs[0]
