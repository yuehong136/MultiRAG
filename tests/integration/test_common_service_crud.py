"""CommonService CRUD 与事务语义的真数据库回归。

单测层的 service 测试全靠 monkeypatch 打桩，测不到 SQL 语义；本文件在
scratch 真库（``bootstrapped_engine``）上验证提交可见性、rowcount、
唯一约束冲突的回滚契约与未提交事务的回滚语义。
"""

import uuid

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from api.db.db_models import User
from api.db.services.user_service import UserService


@pytest.fixture
def session_factory(bootstrapped_engine):
    return sessionmaker(bind=bootstrapped_engine, expire_on_commit=False)


def _payload(**overrides) -> dict:
    payload = {
        "email": f"crud-{uuid.uuid4().hex[:12]}@test.local",
        "nickname": "CRUD Tester",
        "password": "hashed-secret",
    }
    payload.update(overrides)
    return payload


def test_insert_roundtrip_visible_across_sessions(session_factory):
    with session_factory() as db:
        created = UserService.insert(db, **_payload())
        assert created.id and created.create_time and created.create_date

    with session_factory() as db:  # 新会话读取：验证真实提交，而非 identity map 命中
        fetched = UserService.get_by_id(db, created.id)
        assert fetched is not None
        assert fetched.email == created.email


def test_update_by_id_persists_and_reports_rowcount(session_factory):
    with session_factory() as db:
        created = UserService.insert(db, **_payload())

    with session_factory() as db:
        assert UserService.update_by_id(db, created.id, {"nickname": "Renamed"}) == 1
        assert UserService.update_by_id(db, "missing-id", {"nickname": "x"}) == 0

    with session_factory() as db:
        assert UserService.get_by_id(db, created.id).nickname == "Renamed"


def test_delete_by_id_removes_row(session_factory):
    with session_factory() as db:
        created = UserService.insert(db, **_payload())

    with session_factory() as db:
        assert UserService.delete_by_id(db, created.id) == 1
        assert UserService.delete_by_id(db, created.id) == 0

    with session_factory() as db:
        assert UserService.get_by_id(db, created.id) is None


def test_save_duplicate_email_raises_http_500_and_rolls_back(session_factory):
    payload = _payload()
    with session_factory() as db:
        UserService.insert(db, **payload)

    with session_factory() as db:
        # UserService.save 的真实契约：唯一约束冲突 → 内部 rollback → HTTPException(500)
        with pytest.raises(HTTPException) as exc_info:
            UserService.save(db, **_payload(email=payload["email"]))
        assert exc_info.value.status_code == 500
        assert "Integrity error" in exc_info.value.detail
        # 已 rollback：同一会话立即可继续使用，且冲突行未落库
        count = db.execute(sa.select(sa.func.count()).select_from(User).where(User.email == payload["email"])).scalar_one()
        assert count == 1


def test_rollback_discards_uncommitted_changes(session_factory):
    payload = _payload()
    with session_factory() as db:
        # User 子类覆写了 BaseModel.id 且无默认值——直接构造必须显式传主键
        db.add(User(id=uuid.uuid4().hex, **payload))
        db.flush()
        assert db.execute(sa.select(User).where(User.email == payload["email"])).scalar_one_or_none() is not None
        db.rollback()

    with session_factory() as db:
        assert not UserService.query(db, email=payload["email"])
