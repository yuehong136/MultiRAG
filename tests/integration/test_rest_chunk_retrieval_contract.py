"""REST chunk mutations, strict retrieval inputs, and independent SQL/native reads."""

import json
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document
from common import settings
from tests.support.rest_chunk_retrieval_contract import RestChunkContract
from tests.support.rest_chunk_retrieval_contract import rest_chunk_contract as rest_chunk_contract
from tests.support.rest_chunk_retrieval_contract import runtime_upload_api as runtime_upload_api
from tests.support.rest_chunk_retrieval_contract import search_api as search_api


def test_auth_and_invalid_targets_have_zero_mutation(rest_chunk_contract: RestChunkContract) -> None:
    ctx = rest_chunk_contract
    chunk = ctx.add("contract auth")
    before = ctx.snapshot()
    for credential in (None, "invalid-key"):
        for method, path, payload in (
            ("POST", ctx.path, {"content": "denied"}),
            ("GET", ctx.path, None),
            ("GET", ctx.path + "/" + chunk["id"], None),
            ("PATCH", ctx.path + "/" + chunk["id"], {"content": "denied"}),
            ("DELETE", ctx.path, {"delete_all": True}),
            ("POST", "/retrieval", {"question": "contract", "dataset_ids": [ctx.env["dataset"]]}),
        ):
            response = ctx.request(method, path, credential=credential, **({"json": payload} if payload is not None else {}))
            assert response.status_code == 401, response.text
            assert ctx.snapshot() == before
    for path in (ctx.path.replace(ctx.env["dataset"], "missing"), ctx.path.replace(ctx.env["doc"], "missing")):
        for method, suffix, payload in (("POST", "", {"content": "denied"}), ("GET", "", None), ("PATCH", "/" + chunk["id"], {"content": "denied"}), ("DELETE", "", {"chunk_ids": [chunk["id"]]})):
            response = ctx.request(method, path + suffix, **({"json": payload} if payload is not None else {}))
            assert response.status_code == 200 and response.json()["code"] != 0, response.text
            assert ctx.snapshot() == before


def test_keyword_question_tag_types_and_update_roundtrip(rest_chunk_contract: RestChunkContract) -> None:
    ctx = rest_chunk_contract
    for field in ("important_keywords", "questions", "tag_kwd"):
        for i, values in enumerate((["alpha", "beta"], [""], ["alpha", "alpha"])):
            chunk = ctx.add(f"contract {field} {i}", **{field: values})
            expected = [] if field == "questions" and values == [""] else values
            assert chunk[field] == expected
            response = ctx.request("GET", params={"id": chunk["id"]})
            # Native keyword/question reads normalize empty search terms.
            persisted = [] if field in {"important_keywords", "questions"} and values == [""] else expected
            assert response.json()["data"]["chunks"][0][field] == persisted
        for invalid in ([1], "alpha", 123):
            before = ctx.snapshot()
            for method, path, payload in (("POST", ctx.path, {"content": "invalid", field: invalid}), ("PATCH", ctx.path + "/" + chunk["id"], {field: invalid})):
                response = ctx.request(method, path, json=payload)
                assert response.status_code == 422, (field, invalid, response.text)
                assert ctx.snapshot() == before
        patch = {field: ["updated", "updated"]}
        assert ctx.request("PATCH", ctx.path + "/" + chunk["id"], json=patch).json()["code"] == 0
        assert ctx.request("GET", params={"id": chunk["id"]}).json()["data"]["chunks"][0][field] == patch[field]
    for content in ("", " ", "\n\t"):
        before = ctx.snapshot()
        for method, path in (("POST", ctx.path), ("PATCH", ctx.path + "/" + chunk["id"])):
            response = ctx.request(method, path, json={"content": content})
            assert response.json()["code"] == 102
            assert ctx.snapshot() == before
    for available, expected_available in ((False, False), (0, False), (True, True), (1, True), ("False", False), ("True", True)):
        assert ctx.request("PATCH", ctx.path + "/" + chunk["id"], json={"available": available}).json()["code"] == 0
        read = ctx.request("GET", params={"id": chunk["id"]}).json()["data"]["chunks"][0]
        assert read["available"] is expected_available
    for content in ("first replacement", "second replacement", "\n!?。；！？\"'"):
        assert ctx.request("PATCH", ctx.path + "/" + chunk["id"], json={"content": content}).json()["code"] == 0
        read = ctx.request("GET", ctx.path + "/" + chunk["id"]).json()["data"]
        assert read["content_with_weight"] == content
    for payload in ({}, {"unknown_key": "ignored"}):
        assert ctx.request("PATCH", ctx.path + "/" + chunk["id"], json=payload).json()["code"] == 0
        assert ctx.request("GET", ctx.path + "/" + chunk["id"]).json()["data"]["content_with_weight"] == content
    before = ctx.snapshot()
    assert ctx.request("POST", json={"content": None}).status_code == 422
    assert ctx.request("PATCH", ctx.path + "/" + chunk["id"], json={"content": None}).json()["code"] == 102
    assert ctx.request("PATCH", ctx.path + "/missing", json={"content": "denied"}).json()["code"] == 102
    assert ctx.snapshot() == before
    ctx.assert_count(9)


def test_duplicate_and_concurrent_adds_count_native_chunks_once(rest_chunk_contract: RestChunkContract) -> None:
    ctx = rest_chunk_contract
    first, second = ctx.add("contract repeated"), ctx.add("contract repeated")
    assert first["id"] == second["id"]
    ctx.assert_count(1)

    def add(content: str) -> str:
        return ctx.add(content)["id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        repeated = list(pool.map(add, ["contract racing"] * 8))
        unique = list(pool.map(add, [f"contract unique {i}" for i in range(8)]))
    assert len(set(repeated)) == 1 and len(set(unique)) == 8
    ctx.assert_count(10)


@pytest.mark.parametrize("failure", ["increment", "commit_before", "commit_after"])
def test_native_success_sql_failure_is_repaired_by_retry(rest_chunk_contract: RestChunkContract, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    ctx = rest_chunk_contract
    existing = ctx.add("contract preexisting")["id"]
    module = sys.modules["api.apps.restful_apis.chunk"]

    def fail_increment(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Controlled SQL accounting failure")

    class FailingCommitSession(Session):
        def commit(self) -> None:
            if failure == "commit_after":
                super().commit()
            raise RuntimeError("Controlled SQL commit failure")

    @contextmanager
    def failing_db() -> Iterator[Session]:
        with FailingCommitSession(ctx.env["engine"]) as db:
            yield db

    with monkeypatch.context() as patch:
        if failure == "increment":
            patch.setattr(module.DocumentService, "increment_chunk_num", fail_increment)
        else:
            patch.setattr(module, "db_connection", failing_db)
        response = ctx.request("POST", json={"content": "contract recoverable"})
    error = response.json()
    assert response.status_code == 200 and error.get("retcode", error.get("code")) == 100, response.text
    partial = ctx.snapshot()
    assert len(partial["native"]) == 2
    sql_count = 2 if failure == "commit_after" else 1
    assert partial["document"]["chunk_num"] == partial["dataset"]["chunk_num"] == sql_count
    retried = ctx.add("contract recoverable")["id"]
    assert retried != existing
    ctx.assert_count(2)
    assert ctx.request("DELETE", json={"chunk_ids": [existing, retried]}).json()["code"] == 0
    ctx.assert_count(0)


def test_manual_accounting_excludes_legacy_parent_rows(rest_chunk_contract: RestChunkContract) -> None:
    ctx = rest_chunk_contract
    child = ctx.add("contract counted child")["id"]
    parent = uuid4().hex
    ctx.env["record"]["rest_chunk_contract"]["parent_relationship"] = {"parent": parent, "child": child}
    ctx.env["record_path"].write_text(json.dumps(ctx.env["record"]))
    collection, dataset = ctx.env["collections"]["dataset"], ctx.env["dataset"]
    assert settings.docStoreConn.update({"id": child}, {"mom_id": parent}, collection, dataset)
    row = ctx.native()[0]
    # Old mother rows have an empty mom_id and are identified by child refs.
    assert settings.docStoreConn.insert([{**row, "id": parent, "pk": parent, "mom_id": "", "content_with_weight": "contract parent", "available_int": 0}], collection, dataset) == []
    added = ctx.add("contract additional source")["id"]
    state = ctx.snapshot()
    assert {row["pk"] for row in state["native"]} == {parent, child, added}
    assert state["document"]["chunk_num"] == state["dataset"]["chunk_num"] == 2


def test_list_paging_keyword_get_and_concurrent_updates(rest_chunk_contract: RestChunkContract) -> None:
    ctx = rest_chunk_contract
    chunks = [ctx.add(f"contract marker{i}") for i in range(5)]
    ctx.wait_for_count(5)
    ids = {row["id"] for row in chunks}
    paged = [ctx.request("GET", params={"page": page, "page_size": 2}).json()["data"] for page in (1, 2, 3, 4)]
    assert all(page["total"] == 5 for page in paged), paged
    assert [len(page["chunks"]) for page in paged] == [2, 2, 1, 0]
    assert {chunk["id"] for page in paged for chunk in page["chunks"]} == ids
    exact = ctx.request("GET", params={"keywords": "marker1"}).json()["data"]
    assert exact["total"] == 1 and exact["chunks"][0]["id"] == chunks[1]["id"]
    assert ctx.request("GET", params={"keywords": "unmatchedword"}).json()["data"]["total"] == 0
    for field, values in (("page", [0, -1, "bad"]), ("page_size", [0, -1, 101, "bad"])):
        before = ctx.snapshot()
        for value in values:
            assert ctx.request("GET", params={field: value}).status_code == 422
        assert ctx.snapshot() == before
    for suffix, params in (("/missing", {}), ("", {"id": "missing"})):
        response = ctx.request("GET", ctx.path + suffix, params=params)
        assert response.json()["code"] == 102

    def read(_: int) -> dict[str, Any]:
        return ctx.request("GET").json()["data"]

    def update(i: int) -> dict[str, Any]:
        return ctx.request("PATCH", ctx.path + "/" + chunks[i % 5]["id"], json={"content": f"contract update {i}"}).json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(result["total"] == 5 for result in pool.map(read, range(8)))
        assert all(result["code"] == 0 for result in pool.map(update, range(12)))
    assert {row["pk"] for row in ctx.native()} == ids
    assert all(row["content_with_weight"].startswith("contract update ") for row in ctx.native())
    ctx.assert_count(5)


def test_delete_noop_partial_duplicate_concurrent_bulk_and_retrieval(rest_chunk_contract: RestChunkContract) -> None:
    ctx = rest_chunk_contract
    chunks = [ctx.add(f"contract deletion {i}")["id"] for i in range(4)]
    before = ctx.snapshot()
    for payload in ({}, {"chunk_ids": []}, {"chunk_ids": None}):
        assert ctx.request("DELETE", json=payload).json()["code"] == 0
        assert ctx.snapshot() == before
    retrieved = ctx.retrieve().json()
    assert retrieved["code"] == 0 and {row["id"] for row in retrieved["data"]["chunks"]} == set(chunks), retrieved
    result = ctx.request("DELETE", json={"chunk_ids": [chunks[0], "missing", chunks[1]]}).json()
    assert result["code"] == 102 and "deleted chunks 2, expect 3" in result["message"]
    ctx.assert_count(2)
    result = ctx.request("DELETE", json={"chunk_ids": [chunks[2], chunks[2]]}).json()
    assert result["code"] == 0 and result["data"]["success_count"] == 1 and len(result["data"]["errors"]) == 1
    before = ctx.snapshot()
    assert ctx.request("DELETE", json={"chunk_ids": chunks[:3]}).json()["code"] == 102
    assert ctx.snapshot() == before
    assert ctx.request("DELETE", json={"delete_all": True}).json()["code"] == 0
    ctx.assert_count(0)
    assert ctx.retrieve().json()["data"]["chunks"] == []
    ids = [ctx.add(f"contract bulk {i}")["id"] for i in range(40)]
    ctx.assert_count(40)

    def delete(chunk_id: str) -> dict[str, Any]:
        return ctx.request("DELETE", json={"chunk_ids": [chunk_id]}).json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(result["code"] == 0 for result in pool.map(delete, ids[:8]))
    assert ctx.request("DELETE", json={"chunk_ids": ids[8:]}).json()["code"] == 0
    ctx.assert_count(0)
    assert ctx.retrieve().json()["data"]["chunks"] == []


def test_retrieval_parameters_paging_concurrency_and_deleted_document(rest_chunk_contract: RestChunkContract) -> None:
    ctx = rest_chunk_contract
    chunks = [ctx.add(f"contract retrieval {i}")["id"] for i in range(3)]
    ctx.wait_for_count(3)
    for extra in (
        {"page": 0},
        {"page": -1},
        {"page": None},
        {"page": "bad"},
        {"page_size": 0},
        {"page_size": None},
        {"page_size": "bad"},
        {"vector_similarity_weight": 10},
        {"vector_similarity_weight": "nan"},
        {"vector_similarity_weight": "bad"},
        {"top_k": -1},
        {"top_k": "bad"},
        {"highlight": "not_bool"},
    ):
        before = ctx.snapshot()
        response = ctx.retrieve(**extra)
        assert response.status_code == 422, (extra, response.text)
        assert ctx.snapshot() == before
    for extra in (
        {"vector_similarity_weight": 0},
        {"vector_similarity_weight": 0.5},
        {"vector_similarity_weight": 1},
        {"top_k": 1},
        {"highlight": True},
        {"highlight": False},
        {"highlight": "False"},
        {"keyword": False},
        {"keyword": True},
        {"ignored": "value"},
    ):
        response = ctx.retrieve(**extra)
        assert response.status_code == 200 and response.json()["code"] == 0, (extra, response.text)
        assert {row["id"] for row in response.json()["data"]["chunks"]}.issubset(chunks)
    pages = [ctx.retrieve(page=page, page_size=1).json()["data"]["chunks"] for page in (1, 2, 3, 4)]
    assert [len(page) for page in pages] == [1, 1, 1, 0]
    assert {row["id"] for page in pages for row in page} == set(chunks)
    missing_model = ctx.retrieve(rerank_id="missing-model").json()
    assert missing_model.get("retcode", missing_model.get("code")) == 100, missing_model

    def retrieve(_: int) -> dict[str, Any]:
        return ctx.retrieve().json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(response["code"] == 0 and len(response["data"]["chunks"]) == 3 for response in pool.map(retrieve, range(8)))
    # A deleted SQL document must not be writable even when native rows remain.
    with ctx.env["engine"].begin() as db:
        db.execute(sa.delete(Document).where(Document.id == ctx.env["doc"]))
    before = ctx.snapshot()
    for method, suffix in (("POST", ""), ("PATCH", "/" + chunks[0])):
        response = ctx.request(method, ctx.path + suffix, json={"content": "denied after delete"})
        assert response.json()["code"] == 102
        assert ctx.snapshot() == before
    assert ctx.retrieve().json()["data"]["chunks"] == []


def test_dify_prunes_sql_document_deleted_after_native_retrieval(rest_chunk_contract: RestChunkContract, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = rest_chunk_contract
    ctx.add("contract dify deletion race")
    ctx.wait_for_count(1)
    module = sys.modules["api.apps.restful_apis.dify_retrieval"]
    sdk = sys.modules["api.apps.restful_apis.chunk"]
    monkeypatch.setattr(module, "LLMBundle", sdk.LLMBundle)
    monkeypatch.setattr(module, "get_model_config_by_type_and_name", lambda *_: {})
    monkeypatch.setattr(module, "label_question", lambda *_: {})

    def delete_sql_source(chunks: list[dict[str, Any]], tenants: list[str]) -> list[dict[str, Any]]:
        assert chunks and tenants == [ctx.env["owners"][0]]
        with ctx.env["engine"].begin() as db:
            db.execute(sa.delete(Document).where(Document.id == ctx.env["doc"]))
        return chunks

    monkeypatch.setattr(module.settings.retriever, "retrieval_by_children", delete_sql_source)
    response = ctx.request("POST", "/dify/retrieval", credential="sdk_token", json={"knowledge_id": ctx.env["dataset"], "query": "contract"})
    assert response.status_code == 200 and response.json() == {"records": []}, response.text
    assert ctx.snapshot()["document"] is None and len(ctx.native()) == 1
