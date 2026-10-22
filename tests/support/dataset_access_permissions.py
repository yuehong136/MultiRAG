"""Shared real-SQL dataset permission rows with independent cleanup readback."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, UserTenant


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
