from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.db.services.tenant_llm_service import TenantLLMService
from common.parser_config_utils import normalize_layout_recognizer
from core.app import naive
from deepdoc.parser import opendataloader_parser
from deepdoc.parser.opendataloader_parser import OpenDataLoaderParser
from tests.pdf_bbox_support import make_pdf


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("model@factory@oPeNdAtAlOaDeR", ("OpenDataLoader", "model@factory")),
        ("OpenDataLoader", ("OpenDataLoader", None)),
        ("@OpenDataLoader", ("OpenDataLoader", "")),
        ("model@MinerU", ("MinerU", "model")),
        ("model@PaddleOCR", ("PaddleOCR", "model")),
        (False, (False, None)),
        (None, (None, None)),
    ],
)
def test_normalize_parser_model(raw: Any, expected: tuple[Any, str | None]) -> None:
    assert normalize_layout_recognizer(raw) == expected


@pytest.mark.parametrize(
    ("layout", "explicit", "candidates", "selected"),
    [
        ("chosen@factory@oPeNdAtAlOaDeR", "ignored", ["default"], "chosen@factory"),
        ("OpenDataLoader", "explicit", ["default"], "explicit"),
        ("@OpenDataLoader", "explicit", ["default"], "explicit"),
        ("OpenDataLoader", None, ["default"], "default"),
        ("OpenDataLoader", None, [], "environment"),
    ],
)
def test_naive_chunk_routes_model_and_only_supported_pdf_options(monkeypatch: pytest.MonkeyPatch, db: Session, layout: str, explicit: str | None, candidates: list[str], selected: str) -> None:
    parser = OpenDataLoaderParser()
    parser.api_url = "http://opendataloader.test"
    models: list[tuple[str, str, str]] = []
    requests: list[dict[str, Any]] = []
    defaults: list[str] = []

    def ensure(_db: Session, tenant: str) -> str:
        defaults.append(tenant)
        return "environment"

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
    monkeypatch.setattr(TenantLLMService, "ensure_opendataloader_from_env", staticmethod(ensure))
    monkeypatch.setattr(TenantLLMService, "query", classmethod(lambda cls, _db, **kwargs: [SimpleNamespace(llm_name=name) for name in candidates]))
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
    assert defaults == ([] if explicit else ["tenant-test"])
    assert len(requests) == 1
    assert requests[0]["files"]["file"] == ("selected.pdf", binary, "application/pdf")
    assert requests[0]["data"] == {"hybrid": "docling-fast", "image_output": "embedded", "sanitize": "false"}
    assert "Selected model parsed this paragraph." in "\n".join(chunk["content_with_weight"] for chunk in chunks)
