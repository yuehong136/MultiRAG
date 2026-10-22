"""Agent Retrieval authorizes real SQL selections before model or store IO."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from agent.tools import retrieval as module
from agent.tools.retrieval import Retrieval, RetrievalParam
from api.db.db_models import Knowledgebase, TenantLLM
from api.db.joint_services import tenant_model_service as tms
from api.db.services.llm_service import LLMBundle
from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, TenantMembershipEvidence, build_principal_from_authenticated_actor
from api.identity.run_context import RunContext
from tests.integration.test_dataset_access_permissions import permission_rows as permission_rows


@pytest.fixture
def retrieval_scope(bootstrapped_engine: sa.Engine, permission_rows: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    ids = permission_rows
    built: list[dict[str, Any]] = []
    fetched: list[tuple[Any, ...]] = []

    @contextmanager
    def connection() -> Iterator[Session]:
        with Session(bootstrapped_engine) as db:
            yield db

    class ProbeBundle(LLMBundle):
        def __init__(self, db: Session, tenant_id: str, model_config: dict[str, Any], **kwargs: Any) -> None:
            self.db = db
            built.append({"tenant": tenant_id, "config_tenant": model_config["tenant_id"], "model": model_config["id"]})

    async def fetch(*args: Any, **kwargs: Any) -> dict[str, Any]:
        fetched.append(args)
        return {"chunks": [], "doc_aggs": []}

    monkeypatch.setattr(module, "db_connection", connection)
    monkeypatch.setattr(tms, "db_connection", connection)
    monkeypatch.setattr(tms, "LLMBundle", ProbeBundle)
    monkeypatch.setattr(tms.settings, "FACTORY_LLM_INFOS", [{"name": "ScopeFixture"}])
    monkeypatch.setattr(module.settings, "retriever", SimpleNamespace(retrieval=fetch, retrieval_by_children=lambda chunks, _: chunks))
    try:
        with Session(bootstrapped_engine) as db:
            model = TenantLLM(tenant_id=ids["tenant"], llm_factory="ScopeFixture", llm_name="embedding", mdl_type="embedding", api_key="fixture", api_base="http://fixture.invalid")
            db.add(model)
            db.flush()
            assert model.id is not None
            model_id = model.id
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.tenant_id == ids["tenant"]).values(embd_id="embedding@ScopeFixture", tenant_embd_id=model_id))
            for name in ["private", "team"]:
                db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids[name]).values(name=f"{name}-name"))
            db.commit()
        yield {"ids": ids, "engine": bootstrapped_engine, "model": model_id, "built": built, "fetched": fetched}
    finally:
        with Session(bootstrapped_engine) as db:
            db.execute(sa.delete(TenantLLM).where(TenantLLM.tenant_id == ids["tenant"]))
            db.commit()
        with Session(bootstrapped_engine) as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(TenantLLM).where(TenantLLM.tenant_id == ids["tenant"])) == 0


def tool(env: dict[str, Any], actor: str, selected: list[str], *, legacy: bool = False, names: list[str] | None = None) -> Retrieval:
    ids = env["ids"]
    context = None
    if not legacy:
        principal = build_principal_from_authenticated_actor(
            actor=AuthenticatedActor(platform_user_id=ids[actor]),
            membership=TenantMembershipEvidence(platform_user_id=ids[actor], tenant_id=ids["tenant"]),
            authentication=AuthenticationContext(source=AuthenticationSource.WEB_SESSION, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
        )
        context = RunContext(tenant_id=ids["tenant"], principal=principal)
    # The Principal actor differs from the tenant. A forged user_id parameter
    # and a legacy Canvas owner must never override that trusted actor.
    canvas_owner = ids[actor] if legacy else ids["owner"]
    canvas = SimpleNamespace(_tenant_id=ids["tenant"], get_tenant_id=lambda: canvas_owner, get_run_context=lambda: context, get_variable_value=lambda _: names)
    param = RetrievalParam()
    param.dataset_ids = selected
    param.user_id = ids["owner"]
    obj = object.__new__(Retrieval)
    obj._param = param
    obj._canvas = canvas
    obj.outputs = {}
    obj.get_input_elements_from_text = lambda _: {}
    obj.string_format = lambda query, _: query
    obj.check_if_canceled = lambda _: False
    obj.set_output = lambda key, value: obj.outputs.__setitem__(key, value)
    return obj


@pytest.mark.parametrize(("actor", "dataset", "legacy"), [("owner", "private", False), ("admin", "private", False), ("normal", "team", False), ("owner", "private", True)])
@pytest.mark.parametrize("binding", ["id", "name"])
async def test_allowed_retrieval_binds_dataset_model_owner(retrieval_scope: dict[str, Any], monkeypatch: pytest.MonkeyPatch, actor: str, dataset: str, legacy: bool, binding: str) -> None:
    env = retrieval_scope
    ids = env["ids"]
    if binding == "name":
        with Session(env["engine"]) as db:
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids[dataset]).values(tenant_embd_id=None))
            db.commit()
    obj = tool(env, actor, [ids[dataset]], legacy=legacy)
    monkeypatch.setattr(obj, "_rank_feature", lambda *_: {})
    await obj._retrieve_kb("query")
    assert env["built"] == [{"tenant": ids["tenant"], "config_tenant": ids["tenant"], "model": env["model"]}]
    assert len(env["fetched"]) == 1 and env["fetched"][0][3] == [ids["tenant"]]


@pytest.mark.parametrize(
    ("actor", "selection", "legacy"),
    [
        ("normal", ["private"], False),
        ("outsider", ["team"], False),
        ("revoked", ["team"], False),
        ("invite", ["team"], False),
        ("normal", ["invalid"], False),
        ("normal", ["missing"], False),
        ("normal", ["team", "private"], False),
        ("normal", ["team", "missing"], False),
        ("revoked", ["team"], True),
        ("tenant", ["team"], False),
        ("normal", ["variable"], False),
    ],
)
async def test_denied_or_missing_selection_has_zero_downstream_io(retrieval_scope: dict[str, Any], monkeypatch: pytest.MonkeyPatch, actor: str, selection: list[str], legacy: bool) -> None:
    env = retrieval_scope
    ids = env["ids"]
    selected = ["component@datasets" if value == "variable" else ids.get(value, "missing-dataset") for value in selection]
    obj = tool(env, actor, selected, legacy=legacy, names=["private-name", "team-name"])
    obj._param.meta_data_filter = {"method": "auto"}

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Denied dataset reached model/metadata/store IO")

    for name in ["build_bundle_by_id_async", "build_named_bundle_async", "build_default_bundle_async", "apply_meta_data_filter"]:
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(obj, "_flatted_metas", forbidden)
    monkeypatch.setattr(obj, "_rank_feature", forbidden)
    monkeypatch.setattr(module.settings, "retriever", SimpleNamespace(retrieval=forbidden))
    with Session(env["engine"]) as db:
        before = list(db.execute(sa.select(Knowledgebase.__table__).where(Knowledgebase.tenant_id == ids["tenant"])))
    with pytest.raises(PermissionError, match="missing or inaccessible"):
        await obj._retrieve_kb("query")
    assert env["built"] == [] and env["fetched"] == []
    with Session(env["engine"]) as db:
        assert list(db.execute(sa.select(Knowledgebase.__table__).where(Knowledgebase.tenant_id == ids["tenant"]))) == before
        assert db.scalar(sa.select(TenantLLM.used_tokens).where(TenantLLM.id == env["model"])) == 0
