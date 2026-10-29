"""Dify HTTP identity and metadata contracts with real, owned SQL rows."""

import hashlib
import sys
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import APIToken, Document, DocumentMetadata, Knowledgebase, User, UserTenant
from tests.support.agent_update_release import release_api as release_api
from tests.support.resource_journal import ResourceJournal


@pytest.fixture
def dify_http(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = release_api
    module = sys.modules["api.apps.restful_apis.dify_retrieval"]
    owner, member, outsider = env["owners"]
    kbs = {kind: uuid4().hex for kind in ["team", "me"]}
    docs = {author: uuid4().hex for author in ["Ada", "Grace"]}
    membership = uuid4().hex
    tokens = {kind: f"dify-{kind}-{uuid4().hex}" for kind in ["duplicate", "primary", "beta"]}
    tokens["invalid_jwt"] = f"invalid.{uuid4().hex}.signature"
    journal = ResourceJournal("dify-auth-http")
    journal.event(
        "reserve-dify-sql",
        database=env["engine"].url.database,
        host=env["engine"].url.host,
        port=env["engine"].url.port,
        knowledgebase_ids=list(kbs.values()),
        document_ids=list(docs.values()),
        metadata_ids=list(docs.values()),
        membership_id=membership,
        api_tokens=[
            {"tenant_id": tenant, "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
            for tenant, token in [(owner, tokens["duplicate"]), (outsider, tokens["duplicate"]), (owner, tokens["primary"]), (owner, tokens["invalid_jwt"])]
        ],
        beta_sha256=hashlib.sha256(tokens["beta"].encode()).hexdigest(),
    )
    try:
        with Session(env["engine"]) as db:
            db.add(UserTenant(id=membership, tenant_id=owner, user_id=member, invited_by=owner, role="normal", status="1"))
            for permission, identifier in kbs.items():
                db.add(Knowledgebase(id=identifier, tenant_id=owner, created_by=owner, name=f"Dify {permission}", permission=permission, embd_id="controlled-embedding", status="1"))
            for author, identifier in docs.items():
                db.add(Document(id=identifier, kb_id=kbs["team"], created_by=owner, parser_id="naive", type="doc", name=f"{author}.txt", status="1"))
                db.add(DocumentMetadata(id=identifier, tenant_id=owner, kb_id=kbs["team"], meta_fields={"author": author}))
            for tenant in [owner, outsider]:
                db.add(APIToken(tenant_id=tenant, token=tokens["duplicate"], name="dify-duplicate"))
            db.add(APIToken(tenant_id=owner, token=tokens["primary"], beta=tokens["beta"], name="dify-beta"))
            db.add(APIToken(tenant_id=owner, token=tokens["invalid_jwt"], name="dify-invalid-jwt"))
            db.commit()

        async def retrieve(**kwargs: Any) -> dict[str, Any]:
            selected = kwargs["doc_ids"]
            return {
                "chunks": [
                    {"doc_id": identifier, "content_with_weight": author, "similarity": 0.9, "docnm_kwd": f"{author}.txt", "vector": [0.1]}
                    for author, identifier in docs.items()
                    if not selected or identifier in selected
                ]
            }

        metadata = Mock(wraps=module.DocMetadataService.get_flatted_meta_by_kbs)
        model = Mock(return_value={})
        bundle = Mock(return_value=object())
        retriever = SimpleNamespace(retrieval=AsyncMock(side_effect=retrieve), retrieval_by_children=Mock(side_effect=lambda chunks, _tenants: chunks))
        monkeypatch.setattr(module.settings, "DOC_ENGINE", "milvus")
        monkeypatch.setattr(module.settings, "retriever", retriever)
        monkeypatch.setattr(module.DocMetadataService, "get_flatted_meta_by_kbs", metadata)
        monkeypatch.setattr(module, "get_model_config_by_id", model)
        monkeypatch.setattr(module, "get_model_config_by_type_and_name", model)
        monkeypatch.setattr(module, "LLMBundle", bundle)
        monkeypatch.setattr(module, "label_question", Mock(return_value={}))
        yield {**env, "kbs": kbs, "docs": docs, "tokens": tokens, "metadata": metadata, "model": model, "bundle": bundle, "retriever": retriever}
    finally:
        with Session(env["engine"]) as db:
            for model_type, predicate in [
                (DocumentMetadata, DocumentMetadata.id.in_(docs.values())),
                (Document, Document.id.in_(docs.values())),
                (Knowledgebase, Knowledgebase.id.in_(kbs.values())),
                (UserTenant, UserTenant.id == membership),
                (APIToken, sa.and_(APIToken.tenant_id.in_([owner, outsider]), APIToken.token.in_([tokens["duplicate"], tokens["primary"], tokens["invalid_jwt"]]))),
            ]:
                db.execute(sa.delete(model_type).where(predicate))
            db.commit()
        with Session(env["engine"]) as reader:
            remaining = {
                model_type.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model_type).where(predicate))
                for model_type, predicate in [
                    (DocumentMetadata, DocumentMetadata.id.in_(docs.values())),
                    (Document, Document.id.in_(docs.values())),
                    (Knowledgebase, Knowledgebase.id.in_(kbs.values())),
                    (UserTenant, UserTenant.id == membership),
                    (APIToken, APIToken.token.in_([tokens["duplicate"], tokens["primary"], tokens["invalid_jwt"]])),
                ]
            }
            assert not any(remaining.values()), remaining
        journal.event("dify-sql-cleanup-readback", owned_rows_absent=True, remaining=remaining)


def post_retrieval(env: dict[str, Any], credential: str, *, permission: str = "team", author: str | None = None, use_kg: bool = False) -> Any:
    payload: dict[str, Any] = {"knowledge_id": env["kbs"][permission], "query": "controlled question", "use_kg": use_kg}
    if author is not None:
        payload["metadata_condition"] = {"logic": "and", "conditions": [{"name": "author", "comparison_operator": "is", "value": author}]}
    return env["client"].post(env["base"] + "/api/v1/dify/retrieval", headers={"Authorization": f"Bearer {credential}"}, json=payload, timeout=30)


def assert_no_retrieval(env: dict[str, Any]) -> None:
    env["metadata"].assert_not_called()
    env["model"].assert_not_called()
    env["bundle"].assert_not_called()
    env["retriever"].retrieval.assert_not_called()
    env["retriever"].retrieval_by_children.assert_not_called()


@pytest.mark.parametrize("source", ["keys", "jwts"])
def test_real_owner_identity_and_sql_metadata(dify_http: dict[str, Any], source: str) -> None:
    env = dify_http
    response = post_retrieval(env, env[source][0], author="Ada")
    assert response.status_code == 200, response.text
    records = response.json()["records"]
    assert len(records) == 1 and records[0]["content"] == "Ada"
    assert records[0]["metadata"] == {"author": "Ada", "doc_id": env["docs"]["Ada"], "document_id": env["docs"]["Ada"]}
    call = env["retriever"].retrieval.call_args.kwargs
    assert call["tenant_id"] == env["owners"][0] and call["kb_ids"] == [env["kbs"]["team"]]
    assert call["doc_ids"] == [env["docs"]["Ada"]]
    with Session(env["engine"]) as reader:
        assert reader.get(DocumentMetadata, env["docs"]["Ada"]).meta_fields == {"author": "Ada"}


@pytest.mark.parametrize("source", ["keys", "jwts"])
def test_real_member_team_scope_uses_owner_index(dify_http: dict[str, Any], source: str) -> None:
    env = dify_http
    response = post_retrieval(env, env[source][1])
    assert response.status_code == 200 and len(response.json()["records"]) == 2, response.text
    assert env["retriever"].retrieval.call_args.kwargs["tenant_id"] == env["owners"][0]
    assert env["model"].call_args.args[1] == env["owners"][0]
    assert env["bundle"].call_args.args[1] == env["owners"][0]


def test_real_private_scope_denies_before_metadata_and_model(dify_http: dict[str, Any]) -> None:
    response = post_retrieval(dify_http, dify_http["keys"][1], permission="me")
    assert response.status_code == 403 and response.json()["error_code"] == 109, response.text
    assert_no_retrieval(dify_http)


@pytest.mark.parametrize("token_kind", ["duplicate", "invalid_jwt", "beta"])
def test_real_ambiguous_jwt_and_beta_credentials_are_rejected(dify_http: dict[str, Any], token_kind: str) -> None:
    response = post_retrieval(dify_http, dify_http["tokens"][token_kind])
    assert response.status_code == 403 and response.json() == {"error_code": 403, "error_msg": "API-KEY is invalid or missing!"}, response.text
    assert_no_retrieval(dify_http)


@pytest.mark.parametrize("mutation", ["owner", "membership"])
@pytest.mark.parametrize("source", ["keys", "jwts"])
def test_real_disabled_identity_has_zero_retrieval(dify_http: dict[str, Any], mutation: str, source: str) -> None:
    env = dify_http
    owner = env["owners"][0]
    with Session(env["engine"]) as db:
        statement = (
            sa.update(User).where(User.id == owner).values(is_active=False)
            if mutation == "owner"
            else sa.update(UserTenant).where(UserTenant.user_id == owner, UserTenant.tenant_id == owner).values(status="0")
        )
        db.execute(statement)
        db.commit()
    response = post_retrieval(env, env[source][0])
    assert response.status_code == 403 and response.json()["error_code"] == 403, response.text
    assert_no_retrieval(env)


def test_disable_sdk_preserves_real_jwt_identity(dify_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISABLE_SDK", "1")
    denied = post_retrieval(dify_http, dify_http["keys"][0])
    assert denied.status_code == 403 and denied.json()["error_code"] == 403, denied.text
    assert_no_retrieval(dify_http)
    allowed = post_retrieval(dify_http, dify_http["jwts"][0])
    assert allowed.status_code == 200 and len(allowed.json()["records"]) == 2, allowed.text


@pytest.mark.parametrize("use_kg", [False, True])
def test_real_metadata_empty_match_does_not_broaden(dify_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch, use_kg: bool) -> None:
    kg = SimpleNamespace(retrieval=AsyncMock(side_effect=AssertionError("Empty metadata reached KG")))
    module = sys.modules["api.apps.restful_apis.dify_retrieval"]
    monkeypatch.setattr(module.settings, "kg_retriever", kg)
    response = post_retrieval(dify_http, dify_http["keys"][0], author="Nobody", use_kg=use_kg)
    assert response.status_code == 200 and response.json() == {"records": []}, response.text
    kg.retrieval.assert_not_called()
    dify_http["model"].assert_not_called()
    dify_http["bundle"].assert_not_called()
    dify_http["retriever"].retrieval.assert_not_called()
    dify_http["retriever"].retrieval_by_children.assert_not_called()
