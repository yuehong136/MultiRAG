"""Dataset permission policy against committed PostgreSQL rows, sync and async."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.common.check_team_permission import check_kb_team_permission
from api.db.db_models import Document, Knowledgebase, UserTenant
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService


@pytest.fixture
def permission_rows(bootstrapped_engine: sa.Engine) -> Iterator[dict[str, str]]:
    ids = {key: uuid4().hex for key in ["tenant", "creator", "owner", "admin", "normal", "invite", "revoked", "outsider", "private", "team", "invalid", "unknown", "doc", "dead_doc"]}
    with Session(bootstrapped_engine) as db:
        for role in ["owner", "admin", "normal", "invite", "revoked", "creator"]:
            db.add(
                UserTenant(
                    id=uuid4().hex,
                    tenant_id=ids["tenant"],
                    user_id=ids[role],
                    role="normal" if role in {"creator", "revoked"} else role,
                    invited_by=ids["owner"],
                    status="0" if role == "revoked" else "1",
                )
            )
        for key, permission, status in [("private", "me", "1"), ("team", "team", "1"), ("invalid", "team", "0"), ("unknown", "unexpected", "1")]:
            db.add(Knowledgebase(id=ids[key], tenant_id=ids["tenant"], created_by=ids["creator"], name="Shared permission name", embd_id="test", permission=permission, status=status))
        for key, status in [("doc", "1"), ("dead_doc", "0")]:
            db.add(Document(id=ids[key], kb_id=ids["private"], created_by=ids["creator"], name="source.txt", parser_id="naive", type="txt", status=status))
        db.commit()
    yield ids
    with bootstrapped_engine.begin() as db:
        db.execute(sa.delete(Document).where(Document.id.in_([ids["doc"], ids["dead_doc"]])))
        db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id.in_([ids[k] for k in ["private", "team", "invalid", "unknown"]])))
        db.execute(sa.delete(UserTenant).where(UserTenant.tenant_id == ids["tenant"]))
    with bootstrapped_engine.connect() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(Knowledgebase).where(Knowledgebase.tenant_id == ids["tenant"])) == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(UserTenant).where(UserTenant.tenant_id == ids["tenant"])) == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(Document).where(Document.id.in_([ids["doc"], ids["dead_doc"]]))) == 0


@pytest.mark.parametrize("actor", ["creator", "owner", "admin", "normal", "invite", "revoked", "outsider", "tenant"])
async def test_permission_policy_is_shared_by_sync_async_documents_and_lookups(
    bootstrapped_engine: sa.Engine, bootstrapped_async_engine: sa.ext.asyncio.AsyncEngine, permission_rows: dict[str, str], actor: str
) -> None:
    ids = permission_rows
    user = ids[actor]
    private_allowed = actor in {"creator", "owner", "admin"}
    team_allowed = private_allowed or actor == "normal"
    with bootstrapped_engine.connect() as independent:
        before = list(independent.execute(sa.select(Knowledgebase.__table__).where(Knowledgebase.tenant_id == ids["tenant"])))
    with Session(bootstrapped_engine) as db:
        for key, allowed in [("private", private_allowed), ("team", team_allowed), ("invalid", False), ("unknown", False)]:
            assert KnowledgebaseService.accessible(db, ids[key], user) is allowed
            assert bool(KnowledgebaseService.get_kb_by_id(db, ids[key], user)) is allowed
        assert DocumentService.accessible(db, ids["doc"], user) is private_allowed
        assert DocumentService.accessible4deletion(db, ids["doc"], user) is private_allowed
        assert DocumentService.accessible(db, ids["dead_doc"], user) is False
        assert DocumentService.accessible(db, uuid4().hex, user) is False
        assert check_kb_team_permission(db, {"id": ids["private"]}, user) is private_allowed
        found = KnowledgebaseService.get_kb_by_name(db, "Shared permission name", user)
        assert bool(found) is team_allowed
        if found:
            assert found[0]["id"] in {ids["private"], ids["team"]} if private_allowed else found[0]["id"] == ids["team"]
        listed, total = KnowledgebaseService.get_list(db, [ids["tenant"]], user, 1, 100, "id", False, None, None)
        assert {row["id"] for row in listed} == ({ids["private"], ids["team"]} if private_allowed else {ids["team"]} if team_allowed else set())
        assert total == len(listed)
    async with AsyncSession(bootstrapped_async_engine) as db:
        for key, allowed in [("private", private_allowed), ("team", team_allowed), ("invalid", False), ("unknown", False)]:
            assert await KnowledgebaseService.accessible_async(db, ids[key], user) is allowed
    with bootstrapped_engine.connect() as independent:
        assert list(independent.execute(sa.select(Knowledgebase.__table__).where(Knowledgebase.tenant_id == ids["tenant"]))) == before
