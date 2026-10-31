from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.db.joint_services import tenant_model_service
from common.parser_config_utils import normalize_layout_recognizer
from core.app import naive
from deepdoc.parser import opendataloader_parser
from deepdoc.parser.opendataloader_parser import OpenDataLoaderParser
from tests.pdf_bbox_support import make_pdf


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("model@factory@oPeNdAtAlOaDeR", ("OpenDataLoader", "model@factory@oPeNdAtAlOaDeR")),
        ("OpenDataLoader", ("OpenDataLoader", None)),
        ("@OpenDataLoader", ("OpenDataLoader", "@OpenDataLoader")),
        ("model@MinerU", ("MinerU", "model@MinerU")),
        ("model@PaddleOCR", ("PaddleOCR", "model@PaddleOCR")),
        (False, (False, None)),
        (None, (None, None)),
    ],
)
def test_normalize_parser_model(raw: Any, expected: tuple[Any, str | None]) -> None:
    assert normalize_layout_recognizer(raw) == expected


@pytest.mark.parametrize(
    ("layout", "explicit", "candidates", "selected"),
    [
        ("chosen@factory@oPeNdAtAlOaDeR", "ignored", ["41"], "chosen@factory@oPeNdAtAlOaDeR"),
        ("OpenDataLoader", "explicit", ["41"], "explicit"),
        ("OpenDataLoader", None, ["41"], "41"),
        ("OpenDataLoader", None, [], "42"),
    ],
)
def test_naive_chunk_routes_model_and_only_supported_pdf_options(monkeypatch: pytest.MonkeyPatch, db: Session, layout: str, explicit: str | None, candidates: list[str], selected: str) -> None:
    parser = OpenDataLoaderParser()
    parser.api_url = "http://opendataloader.test"
    models: list[tuple[str, str, str]] = []
    requests: list[dict[str, Any]] = []
    defaults: list[tuple[str, str]] = []

    def reference(_db: Session, tenant: str, provider: str) -> str:
        defaults.append((tenant, provider))
        return candidates[0] if candidates else "42"

    def model_config(_db: Session, tenant: str, model_type: str, name: str) -> dict[str, Any]:
        models.append((tenant, model_type, name))
        return {"selected": name}

    def bundle(_db: Session, tenant: str, config: dict[str, Any], lang: str) -> Any:
        assert tenant == "tenant-test" and config == {"selected": selected} and lang == "English"
        return SimpleNamespace(mdl=parser)

    def post(**kwargs: Any) -> Any:
        requests.append(kwargs)
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"json_doc": None, "md_text": "Selected model parsed this paragraph."})

    monkeypatch.setattr(naive, "db_connection", lambda: nullcontext(db))
    monkeypatch.setattr(naive, "get_model_config_by_type_and_name", model_config)
    monkeypatch.setattr(naive, "LLMBundle", bundle)
    monkeypatch.setattr(tenant_model_service, "get_ocr_model_reference", reference)
    monkeypatch.setattr(opendataloader_parser.requests, "post", post)
    binary = make_pdf(1)
    chunks = naive.chunk(
        "selected.pdf",
        binary=binary,
        lang="English",
        callback=lambda *args, **kwargs: None,
        parser_config={"layout_recognize": layout, "chunk_token_num": 0},
        tenant_id="tenant-test",
        opendataloader_llm_name=explicit,
        hybrid="docling-fast",
        image_output="embedded",
        sanitize=False,
        unsupported_option="must not reach parse_pdf",
    )
    assert models == [("tenant-test", "ocr", selected)]
    assert defaults == ([] if explicit else [("tenant-test", "OpenDataLoader")])
    assert len(requests) == 1
    assert requests[0]["files"]["file"] == ("selected.pdf", binary, "application/pdf")
    assert requests[0]["data"] == {"hybrid": "docling-fast", "image_output": "embedded", "sanitize": "false"}
    assert "Selected model parsed this paragraph." in "\n".join(chunk["content_with_weight"] for chunk in chunks)


def test_naive_opendataloader_keeps_rejected_explicit_reference(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    references: list[str] = []

    def rejected(_db: Session, _tenant: str, _model_type: str, name: str) -> dict[str, Any]:
        references.append(name)
        raise LookupError("Invalid or unavailable model reference")

    def unexpected_default(*_args: Any) -> str:
        pytest.fail("An explicit rejected reference must not fall back to another model")

    monkeypatch.setattr(naive, "db_connection", lambda: nullcontext(db))
    monkeypatch.setattr(naive, "get_model_config_by_type_and_name", rejected)
    monkeypatch.setattr(tenant_model_service, "get_ocr_model_reference", unexpected_default)
    assert naive.by_opendataloader("fixture.pdf", tenant_id="tenant-test", opendataloader_llm_name="@OpenDataLoader") == (None, None, None)
    assert references == ["@OpenDataLoader"]
