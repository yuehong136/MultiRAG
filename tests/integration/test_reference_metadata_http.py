"""Real HTTP/auth/SQL/readback and OpenAI SDK; controlled retrieval/model output."""

import json
from typing import Any

import pytest
import requests
from openai import OpenAI
from sqlalchemy.orm import Session

from api.db.db_models import Dialog, Search
from api.db.services import dialog_service
from api.db.services.doc_metadata_service import DocMetadataService
from common import resources
from tests.support.reference_metadata_http import metadata_http as metadata_http
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


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
