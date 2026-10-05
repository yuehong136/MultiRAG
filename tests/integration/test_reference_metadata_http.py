"""Real HTTP/auth/SQL/readback and OpenAI SDK; controlled retrieval/model output."""

import copy
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
import requests
from openai import OpenAI
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from api.apps.services import dataset_search_service
from api.db.db_models import APIToken, Conversation, Dialog, Document, DocumentMetadata, Knowledgebase, Search, get_db
from api.db.services import dialog_service
from api.db.services.doc_metadata_service import DocMetadataService
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
    monkeypatch.setattr(dialog_service.TenantLLMService, "llm_id2llm_type", lambda *a: "chat")
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


def call(env: dict[str, Any], method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    response = requests.request(method, env["base"] + "/api/v1" + path, headers={"Authorization": "Bearer " + env["api_key"]}, timeout=30, **kwargs)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result.get("code") in (0, 200), result
    return result["data"]


def test_metadata_saved_config_http_and_real_openai_sdk(metadata_http: dict[str, Any]) -> None:
    env, ids = metadata_http, metadata_http["ids"]
    selected = {"include": True, "fields": []}
    for path, field in [(f"/chats/{ids['chat']}", "prompt_config"), (f"/searches/{ids['search']}", "search_config")]:
        call(env, "PATCH" if field == "prompt_config" else "PUT", path, json={"name": "Metadata configuration", field: {"reference_metadata": selected}})
        loaded = call(env, "GET", path)
        assert loaded[field]["reference_metadata"] == selected
    with Session(env["engine"]) as db:
        assert db.get(Dialog, ids["chat"]).prompt_config["reference_metadata"] == selected
        assert db.get(Search, ids["search"]).search_config["reference_metadata"] == selected
        # Real SQL store must enforce dataset/document binding, even for valid IDs.
        assert DocMetadataService.get_metadata_for_documents(db, [ids["doc2"]], ids["kb1"]) == {}
    assert call(env, "GET", "/datasets/metadata/keys", params={"dataset_ids": f"{ids['kb1']},{ids['kb2']}"}) == ["author", "secret"]
    search_path = f"/datasets/{ids['kb1']}/search"
    payload = {"question": "source", "dataset_ids": [ids["kb1"], ids["kb2"]], "search_id": ids["search"]}
    assert all("document_metadata" not in c for c in call(env, "POST", search_path, json=payload)["chunks"])
    payload["reference_metadata"] = {"fields": ["author"]}
    result = call(env, "POST", search_path, json=payload)
    assert [c["document_metadata"] for c in result["chunks"]] == [{"author": "author-1"}, {"author": "author-2"}]
    embedded = requests.post(
        env["base"] + "/api/v1/searchbots/retrieval_test",
        headers={"Authorization": "Bearer " + env["beta"]},
        json={"question": "source", "kb_id": payload["dataset_ids"], "search_id": ids["search"], "reference_metadata": {"fields": ["author"]}},
        timeout=30,
    ).json()
    assert embedded.get("retcode") == 0, embedded
    assert [c["document_metadata"] for c in embedded["data"]["chunks"]] == [{"author": "author-1"}, {"author": "author-2"}]

    summary = requests.post(
        env["base"] + f"/api/v1/searches/{ids['search']}/completions",
        headers={"Authorization": "Bearer " + env["api_key"]},
        json={"question": "source", "reference_metadata": {"fields": ["author"]}},
        timeout=30,
    )
    frames = [json.loads(line[5:]) for line in summary.text.splitlines() if line.startswith("data:")]
    assert frames and all(frame["code"] == 0 for frame in frames), summary.text
    references = [frame["data"]["reference"] for frame in frames if isinstance(frame["data"], dict) and frame["data"].get("reference")]
    assert references[-1]["chunks"][0]["document_metadata"] == {"author": "author-1"}

    with OpenAI(api_key=env["api_key"], base_url=env["base"] + f"/api/v1/openai/{ids['chat']}") as sdk:
        for stream in (False, True):
            for preferences in ({"fields": ["author"]}, {"fields": []}, {"fields": None}, {"include": False, "fields": ["author"]}):
                output = sdk.chat.completions.create(model="model", messages=[{"role": "user", "content": "source"}], stream=stream, extra_body={"reference": True, "reference_metadata": preferences})
                if stream:
                    events = list(output)
                    assert events[-1].choices[0].finish_reason == "stop"
                    reference = events[-1].choices[0].delta.model_dump()["reference"]
                    assert sum("reference" in e.choices[0].delta.model_dump() for e in events) == 1
                else:
                    reference = output.choices[0].message.model_dump()["reference"]
                assert len(reference) == 2
                if preferences.get("include") is False or preferences.get("fields") == []:
                    assert all("document_metadata" not in c for c in reference)
                    assert "author-1" not in env["prompts"][-1]
                else:
                    assert reference[0]["document_metadata"]["author"] == "author-1"
                    assert reference[1]["document_metadata"]["author"] == "author-2"
                    assert ("secret" in reference[0]["document_metadata"]) == (preferences["fields"] is None)
                    assert "author-1" in env["prompts"][-1]

    for stream in (False, True):
        response = requests.post(
            env["base"] + "/api/v1/chat/completions",
            headers={"Authorization": "Bearer " + env["api_key"]},
            json={"chat_id": ids["chat"], "messages": [{"role": "user", "content": "source"}], "stream": stream, "reference_metadata": {"fields": ["author"]}},
            timeout=30,
        )
        if stream:
            events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
            assert all(e["code"] == 0 for e in events), events
            answer = next(e["data"] for e in reversed(events) if isinstance(e["data"], dict) and e["data"].get("reference"))
        else:
            result = response.json()
            assert result["code"] == 0, result
            answer = result["data"]
        assert answer["reference"]["chunks"][0]["document_metadata"] == {"author": "author-1"}
    env["record"]["reference_metadata_acceptance"] = {
        "save_reload": True,
        "sql_pair_filter": True,
        "keys": True,
        "search": True,
        "search_summary": True,
        "embedded": True,
        "openai_sdk_cases": 8,
        "ordinary_chat_modes": 2,
    }


def test_metadata_keys_authorization_and_read_failure(metadata_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, ids = metadata_http, metadata_http["ids"]
    response = requests.get(env["base"] + "/api/v1/datasets/metadata/keys", headers={"Authorization": "Bearer " + env["api_key"]}, params={"dataset_ids": ids["kb1"] + ",missing"}, timeout=30)
    assert response.json()["code"] not in (0, 200), response.text

    async def failed(*args: Any, **kwargs: Any) -> dict[str, dict]:
        raise RuntimeError("controlled metadata store failure")

    monkeypatch.setattr(DocMetadataService, "get_metadata_for_documents_async", failed)
    response = requests.get(env["base"] + "/api/v1/datasets/metadata/keys", headers={"Authorization": "Bearer " + env["api_key"]}, params={"dataset_ids": ids["kb1"]}, timeout=30)
    assert response.json()["code"] not in (0, 200), response.text
    assert response.json().get("data") != []


@pytest.mark.parametrize("aggregate", [False, True])
def test_sql_references_through_openai_sdk(metadata_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch, aggregate: bool) -> None:
    env, ids = metadata_http, metadata_http["ids"]

    async def generate_sql(*args: Any, **kwargs: Any) -> str:
        return "select count(*) from source" if aggregate else "select doc_id, docnm_kwd, kb_id, value from source"

    def sql_retrieval(sql: str, **kwargs: Any) -> dict[str, Any]:
        if "count(*)" in sql:
            return {"columns": [{"name": "count(*)"}], "rows": [[2]]}
        return {"columns": [{"name": name} for name in ("doc_id", "docnm_kwd", "kb_id", "value")], "rows": [[ids[f"doc{i}"], f"{i}.txt", ids[f"kb{i}"], i] for i in (1, 2)]}

    monkeypatch.setattr(env["model"], "async_chat", generate_sql)
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "get_field_map", lambda *args: {"value": "Value"})
    monkeypatch.setattr(resources._state["retriever"], "sql_retrieval", sql_retrieval, raising=False)
    with OpenAI(api_key=env["api_key"], base_url=env["base"] + f"/api/v1/openai/{ids['chat']}") as sdk:
        for stream in (False, True):
            result = sdk.chat.completions.create(model="model", messages=[{"role": "user", "content": "show values"}], stream=stream, extra_body={"reference": True})
            if stream:
                events = list(result)
                assert events[-1].choices[0].finish_reason == "stop"
                refs = events[-1].choices[0].delta.model_dump()["reference"]
            else:
                refs = result.choices[0].message.model_dump()["reference"]
            assert [c["document_metadata"] for c in refs] == [{"author": "author-1"}, {"author": "author-2"}]
