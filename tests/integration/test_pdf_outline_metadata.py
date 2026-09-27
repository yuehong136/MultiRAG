from io import BytesIO
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from pypdf import PdfWriter
from reportlab.pdfgen import canvas
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase
from api.db.services.doc_metadata_service import DocMetadataService
from common import settings
from core.svr import task_executor


def pdf_bytes(*, bookmarked: bool) -> bytes:
    content = BytesIO()
    pdf = canvas.Canvas(content)
    pdf.drawString(72, 720, "Chapter One content for indexing.")
    pdf.save()
    if not bookmarked:
        return content.getvalue()
    writer = PdfWriter()
    writer.append(BytesIO(content.getvalue()))
    writer.add_outline_item("Chapter One", 0)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("bookmarked", [True, False])
@pytest.mark.parametrize("path", ["standard", "dataflow"])
async def test_pdf_parse_persists_outline_in_scratch_metadata(
    bootstrapped_engine: sa.Engine,
    monkeypatch: pytest.MonkeyPatch,
    bookmarked: bool,
    path: str,
) -> None:
    tenant_id = uuid4().hex
    kb_id = uuid4().hex
    doc_id = uuid4().hex
    binary = pdf_bytes(bookmarked=bookmarked)
    monkeypatch.setattr(settings, "DOC_ENGINE", "milvus")

    def get_address(db: Session, doc_id: str) -> tuple[str, str]:
        return "scratch", "outline.pdf"

    async def get_binary(bucket: str, name: str) -> bytes:
        return binary

    monkeypatch.setattr(task_executor.File2DocumentService, "get_storage_address", get_address)
    monkeypatch.setattr(task_executor, "get_storage_binary", get_binary)

    task: dict[str, Any] = {
        "id": uuid4().hex,
        "doc_id": doc_id,
        "kb_id": kb_id,
        "tenant_id": tenant_id,
        "parser_id": "naive",
        "name": "outline.pdf",
        "location": "outline.pdf",
        "size": len(binary),
        "from_page": 0,
        "to_page": 1,
        "language": "English",
        "parser_config": {"layout_recognize": "Plain Text", "chunk_token_num": 128, "delimiter": "\n", "analyze_hyperlink": False},
        "kb_parser_config": {},
    }

    def progress(*args: Any, **kwargs: Any) -> None:
        pass

    with Session(bootstrapped_engine) as db:
        db.add(Knowledgebase(id=kb_id, tenant_id=tenant_id, name="outline scratch", created_by=tenant_id, embd_id="test"))
        db.add(Document(id=doc_id, kb_id=kb_id, parser_id="naive", type="pdf", created_by=tenant_id, name="outline.pdf", size=len(binary)))
        db.commit()
        assert DocMetadataService.update_document_metadata(db, doc_id, {"source": "scratch", "tags": ["existing"]})

        if path == "standard":
            chunks = await task_executor.build_chunks(task, progress, db)
            assert chunks
            assert all("__outline__" not in chunk for chunk in chunks)
        else:
            assert await task_executor._persist_pdf_outline_from_storage(db, doc_id, "outline.pdf") is bookmarked

    with Session(bootstrapped_engine) as db:
        metadata = DocMetadataService.get_document_metadata(db, doc_id)
        assert metadata["source"] == "scratch"
        assert metadata["tags"] == ["existing"]
        if bookmarked:
            assert metadata["outline"] == [{"title": "Chapter One", "depth": 0}]
        else:
            assert "outline" not in metadata

        assert DocMetadataService.delete_document_metadata(db, doc_id, kb_id, tenant_id)
        db.delete(db.get(Document, doc_id))
        db.delete(db.get(Knowledgebase, kb_id))
        db.commit()
        assert DocMetadataService.get_document_metadata(db, doc_id) == {}
