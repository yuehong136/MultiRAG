"""Search detail filters missing/inactive rows before serializing SQL results."""

from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Search, User
from api.db.services.search_service import SearchService


@pytest.mark.parametrize("state", ["valid", "missing", "inactive-search", "inactive-owner", "missing-owner"])
def test_detail_active_rows_only(pg_scratch_engine: sa.Engine, state: str) -> None:
    User.__table__.create(pg_scratch_engine, checkfirst=True)
    Search.__table__.create(pg_scratch_engine, checkfirst=True)
    owner, search_id = uuid4().hex, uuid4().hex
    with Session(pg_scratch_engine) as writer:
        if state != "missing-owner":
            writer.add(User(id=owner, nickname="Owner", email=f"{owner}@example.test", status="0" if state == "inactive-owner" else "1"))
        if state != "missing":
            writer.add(Search(id=search_id, tenant_id=owner, created_by=owner, name="Guard test", status="0" if state == "inactive-search" else "1", search_config={"top_k": 12}))
        writer.commit()
    with Session(pg_scratch_engine) as reader:
        detail = SearchService.get_detail(reader, search_id)
        if state == "valid":
            assert detail["id"] == search_id
            assert detail["nickname"] == "Owner"
            assert detail["search_config"] == {"top_k": 12}
        else:
            assert detail == {}
    with pg_scratch_engine.connect() as independent_reader:
        expected = 0 if state == "missing" else 1
        assert independent_reader.scalar(sa.select(sa.func.count()).select_from(Search).where(Search.id == search_id)) == expected


def test_database_failure_propagates(pg_scratch_engine: sa.Engine) -> None:
    User.__table__.create(pg_scratch_engine, checkfirst=True)
    Search.__table__.create(pg_scratch_engine, checkfirst=True)
    with Session(pg_scratch_engine) as reader:
        with pytest.raises(sa.exc.DBAPIError):
            reader.execute(sa.text("SELECT 1 / 0"))
        with pytest.raises(sa.exc.DBAPIError):
            SearchService.get_detail(reader, "missing")
        reader.rollback()
        assert SearchService.get_detail(reader, "missing") == {}
