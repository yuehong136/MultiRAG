"""Shared scratch HTTP fixture with controlled reference-aware model output."""

import copy
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from api.apps.services import dataset_search_service
from api.db.db_models import APIToken, Conversation, Dialog, Document, DocumentMetadata, Knowledgebase, Search, get_db
from api.db.services import dialog_service
from common import resources
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def metadata_http(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    from api.apps import app

    env = runtime_upload_api
    owner = env["owners"][1]
    ids = {key: uuid4().hex for key in ("kb1", "kb2", "doc1", "doc2", "chat", "search")}
    beta = "metadata-" + uuid4().hex
    prompt = {"system": "Answer from {knowledge}", "parameters": [{"key": "knowledge", "optional": False}], "quote": True, "reference_metadata": {"include": True, "fields": ["author"]}}
    with Session(env["engine"]) as db:
        for number in (1, 2):
            kb, doc = ids[f"kb{number}"], ids[f"doc{number}"]
            db.add(Knowledgebase(id=kb, tenant_id=owner, created_by=owner, name=kb, embd_id="controlled", parser_id="naive", chunk_num=1))
            db.add(Document(id=doc, kb_id=kb, created_by=owner, name=f"{number}.txt", type="txt", parser_id="naive"))
            db.add(DocumentMetadata(id=doc, kb_id=kb, tenant_id=owner, meta_fields={"author": f"author-{number}", "secret": f"private-{number}"}))
        db.add(Dialog(id=ids["chat"], tenant_id=owner, name="Metadata chat", llm_id="controlled", kb_ids=[ids["kb1"], ids["kb2"]], prompt_config=prompt, llm_setting={}))
        db.add(
            Search(
                id=ids["search"],
                tenant_id=owner,
                created_by=owner,
                name="Metadata search",
                search_config={"kb_ids": [ids["kb1"], ids["kb2"]], "reference_metadata": {"include": True, "fields": ["author"]}},
            )
        )
        db.query(APIToken).filter(APIToken.token == env["api_key"]).update({"beta": beta})
        db.commit()

    def scratch_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    monkeypatch.setitem(app.dependency_overrides, get_db, scratch_db)
    chunks = [
        {"chunk_id": f"chunk-{i}", "kb_id": ids[f"kb{i}"], "doc_id": ids[f"doc{i}"], "docnm_kwd": f"{i}.txt", "content_with_weight": f"source {i}", "content_ltks": f"source {i}", "vector": [1.0]}
        for i in (1, 2)
    ]
    prompts: list[str] = []

    class Model:
        db = None
        max_length = 8192

        async def async_chat(self, system: str, *args: Any, **kwargs: Any) -> str:
            prompts.append(system)
            return "Answer [ID:0] [ID:1]"

        async def async_chat_streamly_delta(self, system: str, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
            prompts.append(system)
            yield "Answer "
            yield "[ID:0] [ID:1]"

    class Retriever:
        async def retrieval(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"total": 2, "chunks": copy.deepcopy(chunks), "doc_aggs": [{"doc_id": c["doc_id"], "doc_name": c["docnm_kwd"], "count": 1} for c in chunks]}

        def retrieval_by_children(self, chunks: list[dict[str, Any]], *args: Any) -> list[dict[str, Any]]:
            return chunks

        def insert_citations(self, answer: str, *args: Any, **kwargs: Any) -> tuple[str, set[str]]:
            return answer, {"0", "1"}

    model = Model()

    def models(db: Session, dialog: Any) -> tuple[Any, ...]:
        kbs = list(db.scalars(select(Knowledgebase).where(Knowledgebase.id.in_(dialog.kb_ids))))
        return kbs, model, None, model, None

    async def bundle(*args: Any, **kwargs: Any) -> Any:
        return model

    monkeypatch.setitem(resources._state, "retriever", Retriever())
    monkeypatch.setattr(dialog_service, "get_models", models)
    monkeypatch.setattr(dialog_service, "_resolve_dialog_primary_model_config", lambda *a: {"max_tokens": 8192, "llm_name": "controlled"})
    monkeypatch.setattr(dialog_service, "resolve_model_type", lambda *a: ["chat"])
    monkeypatch.setattr(dialog_service.TenantLangfuseService, "filter_by_tenant", lambda *a, **k: None)
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "get_field_map", lambda *a: {})
    monkeypatch.setattr(dialog_service, "label_question", lambda *a: {})
    monkeypatch.setattr(dialog_service, "_resolve_model_config", lambda *a: {})
    monkeypatch.setattr(dialog_service, "get_model_config_by_type_and_name", lambda *a: {})
    monkeypatch.setattr(dialog_service, "LLMBundle", lambda *a: model)
    monkeypatch.setattr(dataset_search_service, "label_question", lambda *a: {})
    monkeypatch.setattr(dataset_search_service, "_bundle", bundle)
    import sys

    embedded = sys.modules["api.apps.sdk.session"]
    monkeypatch.setattr(embedded, "build_named_bundle_async", bundle)
    monkeypatch.setattr(embedded, "_label_question_with_conn", lambda *a: {})
    try:
        yield {**env, "ids": ids, "beta": beta, "prompts": prompts, "chunks": chunks, "model": model}
    finally:
        with Session(env["engine"]) as db:
            db.execute(delete(Conversation).where(Conversation.dialog_id == ids["chat"]))
            for model_type, column, values in [
                (Dialog, Dialog.id, [ids["chat"]]),
                (Search, Search.id, [ids["search"]]),
                (DocumentMetadata, DocumentMetadata.id, [ids["doc1"], ids["doc2"]]),
                (Document, Document.id, [ids["doc1"], ids["doc2"]]),
                (Knowledgebase, Knowledgebase.id, [ids["kb1"], ids["kb2"]]),
            ]:
                db.execute(delete(model_type).where(column.in_(values)))
            db.commit()
