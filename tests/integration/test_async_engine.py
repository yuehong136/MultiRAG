"""AsyncEngine/AsyncSession 真库行为测试（纯异步改造 Phase 0 验收）。

驱动与同步侧同为 psycopg3；库由 bootstrapped_engine 引导（建表 + stamp head）。
"""

import uuid

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.db.db_models import User


async def test_async_engine_select_one(bootstrapped_async_engine):
    async with bootstrapped_async_engine.connect() as conn:
        assert (await conn.execute(sa.text("SELECT 1"))).scalar() == 1


async def test_async_session_orm_roundtrip(bootstrapped_async_engine):
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    email = f"async-{uuid.uuid4().hex[:12]}@test.local"
    user_id = uuid.uuid4().hex

    async with factory() as session:
        session.add(User(id=user_id, email=email, nickname="Async Tester", password="hashed-secret"))
        await session.commit()
        # expire_on_commit=False：commit 后属性访问不触发隐式 IO（异步下会抛 MissingGreenlet）

    async with factory() as session:  # 新会话读取：验证真实提交，而非 identity map 命中
        fetched = await session.get(User, user_id)
        assert fetched is not None
        assert fetched.email == email
        await session.delete(fetched)
        await session.commit()

    async with factory() as session:
        gone = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        assert gone is None
