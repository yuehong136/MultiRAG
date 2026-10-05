"""Metadata selection must preserve request presence and source identity."""

from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from api.db.services.dialog_service import _sql_reference_chunk
from api.db.services.doc_metadata_service import DocMetadataService
from api.utils.reference_metadata import ReferenceMetadata, enrich_reference_metadata, resolve_reference_metadata_preferences
from core.prompts.generator import chunks_format, kb_prompt


@pytest.mark.parametrize(("overrides", "expected"), [({}, (True, {"author"})), ({"include": False}, (False, {"author"})), ({"fields": []}, (True, set())), ({"fields": None}, (True, None))])
def test_presence_and_override(overrides: dict[str, Any], expected: tuple[bool, set[str] | None]) -> None:
    payload = {"reference_metadata": ReferenceMetadata.model_validate(overrides).model_dump()}
    assert resolve_reference_metadata_preferences(payload, {"reference_metadata": {"include": True, "fields": ["author"]}}) == expected


@pytest.mark.parametrize("value", [{"include": "false"}, {"fields": "author"}, {"fields": [1]}])
def test_invalid_preferences(value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        resolve_reference_metadata_preferences({"reference_metadata": value})


def test_dataset_document_pairs_and_no_ambiguous_enrichment(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def metadata(s: Session, docs: list[str], kb: str) -> dict[str, Any]:
        calls.append((kb, docs))
        return {"same": {"owner": kb, "private": "hidden"}}

    monkeypatch.setattr(DocMetadataService, "get_metadata_for_documents", metadata)
    chunks = [{"kb_id": kb, "doc_id": "same"} for kb in ["a", ["b"], ["a", "b"], None]]
    enrich_reference_metadata(db, chunks, (True, {"owner"}))
    assert [chunk.get("document_metadata") for chunk in chunks] == [{"owner": "a"}, {"owner": "b"}, None, None]
    assert calls == [("a", ["same"]), ("b", ["same"])]
    assert [c.get("document_metadata") for c in chunks_format({"chunks": chunks})] == [{"owner": "a"}, {"owner": "b"}, None, None]
    calls.clear()
    enrich_reference_metadata(db, chunks, (True, set()))
    assert not calls
    assert all("document_metadata" not in c for c in chunks)


@pytest.mark.parametrize(
    "kb_ids,row,expected", [(["a"], ["doc", "title"], "a"), (["a", "b"], ["doc", "title"], None), (["a", "b"], ["doc", "title", "b"], "b"), (["a"], ["doc", "title", "other"], None)]
)
def test_sql_pair_provenance(kb_ids: list[str], row: list[str], expected: str | None) -> None:
    columns = [{"name": name} for name in ["DOC_ID", "DOCNM", "KB_ID"]]
    assert _sql_reference_chunk(columns, row, kb_ids).get("kb_id") == expected


def test_prompt_only_consumes_selected_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Prompt rendering must not fetch metadata")

    monkeypatch.setattr(DocMetadataService, "get_metadata_for_documents", forbidden)
    chunks = [{"content_with_weight": "text", "doc_id": "same", "kb_id": kb, "document_metadata": meta} for kb, meta in [("a", {"author": "Alice"}), ("b", {"author": "Bob"}), ("c", None)]]
    prompts = kb_prompt({"chunks": chunks}, 1000)
    assert "Alice" in prompts[0] and "Bob" not in prompts[0]
    assert "Bob" in prompts[1] and "Alice" not in prompts[1]
    assert "author" not in prompts[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("aggregate", [False, True])
async def test_sql_references_retain_dataset_pairs(monkeypatch: pytest.MonkeyPatch, db: Session, aggregate: bool) -> None:
    from types import SimpleNamespace

    from api.db.services import dialog_service
    from common import resources

    queries: list[str] = []
    columns = [{"name": name} for name in ["doc_id", "docnm_kwd", "kb_id", "value"]]
    source_table = {"columns": columns, "rows": [["same", "A", "a", 1], ["same", "B", "b", 2]]}

    def retrieve(sql: str, **kwargs: Any) -> dict[str, Any]:
        queries.append(sql)
        if aggregate and len(queries) == 1:
            return {"columns": [{"name": "count(*)"}], "rows": [[2]]}
        return source_table

    class Model:
        async def async_chat(self, *args: Any, **kwargs: Any) -> str:
            return "SELECT count(*) FROM table" if aggregate else "SELECT doc_id, docnm_kwd, kb_id, value FROM table"

    monkeypatch.setattr(dialog_service.settings, "DOC_ENGINE", "elasticsearch")
    monkeypatch.setitem(resources._state, "retriever", SimpleNamespace(sql_retrieval=retrieve))
    monkeypatch.setattr(dialog_service, "index_name", lambda *a: "table")
    monkeypatch.setattr(DocMetadataService, "get_metadata_for_documents", lambda s, docs, kb: {"same": {"owner": kb}})
    result = await dialog_service.use_sql("show values", {"value": "Value"}, "tenant", ["A", "B"], Model(), kb_ids=["a", "b"])
    assert result is not None
    chunks = result["reference"]["chunks"]
    enrich_reference_metadata(db, chunks, (True, None))
    assert [c["document_metadata"] for c in chunks] == [{"owner": "a"}, {"owner": "b"}]
    assert "|kb_id|" not in result["answer"]
    if aggregate:
        assert "kb_id" in queries[-1].split("from")[0]
