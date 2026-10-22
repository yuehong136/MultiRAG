"""Dataset permission policy against committed PostgreSQL rows, sync and async."""

from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.common.check_team_permission import check_kb_team_permission
from api.db.db_models import Knowledgebase
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from tests.support.dataset_access_permissions import permission_rows as permission_rows


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
