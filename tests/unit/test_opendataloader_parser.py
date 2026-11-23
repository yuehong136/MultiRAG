from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from PIL import Image
from sqlalchemy.orm import Session

import deepdoc.parser.opendataloader_parser as opendataloader_module
from api.db.db_models import TenantModel, TenantModelInstance, TenantModelProvider
from api.db.services.tenant_llm_service import TenantLLMService
from core.llm import OcrModel
from deepdoc.parser.opendataloader_parser import OpenDataLoaderParser


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = "ok"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


def _parser() -> OpenDataLoaderParser:
    parser = OpenDataLoaderParser()
    parser.api_url = "http://opendataloader.local"
    return parser


def test_ocr_registry_exposes_opendataloader() -> None:
    assert OcrModel["OpenDataLoader"].__name__ == "OpenDataLoaderOcrModel"


def test_parse_pdf_posts_binary_and_preserves_structured_sections(monkeypatch) -> None:
    parser = _parser()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(opendataloader_module, "extract_pdf_outlines", lambda _source: [])
    monkeypatch.setattr(parser, "__images__", lambda *_args, **_kwargs: None)

    def fake_post(**kwargs: Any) -> _Response:
        calls.append(kwargs)
        return _Response(
            {
                "json_doc": {
                    "type": "paragraph",
                    "content": "Hello from OpenDataLoader",
                    "page_number": 1,
                    "bounding_box": [0, 0, 100, 20],
                },
                "md_text": None,
            }
        )

    monkeypatch.setattr(opendataloader_module.requests, "post", fake_post)

    sections, tables = parser.parse_pdf("document.pdf", binary=b"%PDF", parse_method="pipeline", sanitize=True)

    assert sections == [("Hello from OpenDataLoader", "paragraph", "")]
    assert tables == []
    assert calls[0]["url"] == "http://opendataloader.local/file_parse"
    assert calls[0]["files"]["file"] == ("document.pdf", b"%PDF", "application/pdf")
    assert calls[0]["data"] == {"sanitize": "true"}


def test_parse_pdf_falls_back_to_markdown(monkeypatch) -> None:
    parser = _parser()
    monkeypatch.setattr(opendataloader_module, "extract_pdf_outlines", lambda _source: [])
    monkeypatch.setattr(parser, "__images__", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        opendataloader_module.requests,
        "post",
        lambda **_kwargs: _Response({"json_doc": None, "md_text": "# Heading"}),
    )

    sections, tables = parser.parse_pdf("document.pdf", binary=BytesIO(b"%PDF"), parse_method="pipeline")

    assert sections == [("# Heading", "text", "")]
    assert tables == []


def test_crop_rejects_out_of_range_page_tag() -> None:
    parser = _parser()
    parser.page_images = [Image.new("RGB", (200, 300))]

    assert parser.crop("@@5\t10.0\t100.0\t20.0\t80.0##") is None


def test_opendataloader_env_config_creates_ocr_model(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    monkeypatch.setenv("OPENDATALOADER_APISERVER", "http://opendataloader.local")
    saved: list[TenantModel | TenantModelInstance | TenantModelProvider] = []
    monkeypatch.setattr(db, "scalar", Mock(side_effect=["tenant-1", None]))
    monkeypatch.setattr(db, "scalars", Mock(return_value=[]))
    monkeypatch.setattr(db, "execute", Mock(return_value=SimpleNamespace(all=lambda: [])))
    monkeypatch.setattr(db, "add", saved.append)

    def flush() -> None:
        if saved and isinstance(saved[-1], TenantModel):
            saved[-1].id = "a" * 32

    monkeypatch.setattr(db, "flush", flush)
    assert TenantLLMService.ensure_opendataloader_from_env(db, "tenant-1") == "a" * 32
    provider, instance, model = saved
    assert isinstance(provider, TenantModelProvider) and isinstance(instance, TenantModelInstance) and isinstance(model, TenantModel)
    assert (provider.tenant_id, provider.provider_name) == ("tenant-1", "OpenDataLoader")
    assert (model.model_name, model.model_types, model.max_tokens) == ("opendataloader-from-env-1", ["ocr"], 0)
    assert model.instance_id == instance.id and model.provider_id == provider.id
    assert "http://opendataloader.local" in instance.api_key


def test_opendataloader_env_config_reuses_matching_model(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    monkeypatch.setenv("OPENDATALOADER_APISERVER", "http://opendataloader.local")
    identifier = "a" * 32
    model = TenantModel(id=identifier, model_name="configured-odl", model_type=64, status="active")
    instance = TenantModelInstance(api_key='{"OPENDATALOADER_APISERVER": "http://opendataloader.local"}', status="active")
    provider = TenantModelProvider(status="active")
    monkeypatch.setattr(db, "scalar", Mock(side_effect=["tenant-1", provider]))
    monkeypatch.setattr(db, "execute", Mock(return_value=SimpleNamespace(all=lambda: [(model, instance, provider)])))
    add = Mock(side_effect=AssertionError("must reuse existing model"))
    monkeypatch.setattr(db, "add", add)
    assert TenantLLMService.ensure_opendataloader_from_env(db, "tenant-1") == identifier
    add.assert_not_called()
