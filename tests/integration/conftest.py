"""Real database fixtures and dependency-scoped service readiness.

The Makefile runner prepares endpoints before importing application modules.
Direct pytest remains supported; missing services skip locally and fail under
REQUIRE_SERVICES=1. See tests/README.md for suites and ownership boundaries.
"""

import asyncio
import os
import sys
import uuid
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any

import pytest
import sqlalchemy as sa

from common.config_utils import CONFIGS
from tests.support import services
from tests.support.database import _alembic_config as _alembic_config
from tests.support.database import _pg_role_can_create_db as _pg_role_can_create_db
from tests.support.database import _pg_url as _pg_url
from tests.support.integration_suites import required_services


def _docker_usable() -> bool:
    """探测级②的准入：显式关闭开关 > docker daemon 可达性。"""
    if os.environ.get("INTEGRATION_NO_TESTCONTAINERS", "").lower() in {"1", "true", "yes"}:
        return False
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    """覆盖 pytest-asyncio 的同名 fixture：Windows 上必须是 selector 循环。

    psycopg 3 的 async 连接在 ``sys.platform == "win32"`` 且循环是 ``ProactorEventLoop``
    时直接抛 ``InterfaceError``（``psycopg/connection_async.py`` 里的显式断言），而
    ``asyncio_mode = "auto"`` 下 pytest-asyncio 拿到的正是 Windows 默认的 Proactor 循环 ——
    于是本目录几乎每条真库用例都在建连处就死掉，且失败信息与被测逻辑毫无关系。

    走 pytest-asyncio 的 ``event_loop_policy`` seam 而不是在 import 期调
    ``asyncio.set_event_loop_policy()``：插件用 ``_temporary_event_loop_policy``
    把策略只装在它自己的 ``Runner`` 上并在收尾时还原，进程全局状态不会被测试套件污染。
    等 pytest-asyncio 提供 ``loop_factory`` seam（1.3.0 还没有）后，这里应改成直接返回
    ``asyncio.SelectorEventLoop``，那才是 3.14 弃用策略体系之后的写法。

    Linux/macOS 的默认循环本来就是 selector 系（epoll/kqueue），psycopg 无此限制，
    所以那边显式返回默认策略，等价于不覆盖。分支用 ``sys.platform`` 而不是 ``os.name``
    ——只有前者会被类型检查器当作平台守卫，``WindowsSelectorEventLoopPolicy``
    在非 Windows 的 typeshed 里并不存在。

    代价：Windows 的 selector 循环基于 ``select()``，句柄数上限 512，且不支持
    ``asyncio`` 子进程。集成套件两者都用不到；真要加用子进程的异步用例，
    得单独给它一个 Proactor 循环，而不是把这里改回去。
    """

    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="session")
def service_manager() -> Iterator[services.ServiceManager]:
    if services.ACTIVE_MANAGER is not None:
        yield services.ACTIVE_MANAGER
    else:
        with services.ServiceManager(CONFIGS) as manager:
            yield manager


def _ensure_service(manager: services.ServiceManager, name: str) -> None:
    try:
        manager.ensure(name)
    except services.ServiceError as exc:
        if os.environ.get("REQUIRE_SERVICES"):
            pytest.fail(str(exc), pytrace=False)
        pytest.skip(str(exc))


@pytest.fixture(scope="session")
def postgres_service(service_manager: services.ServiceManager) -> None:
    _ensure_service(service_manager, "postgresql")


@pytest.fixture(scope="module", autouse=True)
def _require_services(request: pytest.FixtureRequest, service_manager: services.ServiceManager) -> None:
    for service in sorted(required_services([request.node.path])):
        _ensure_service(service_manager, service)


@pytest.fixture(scope="session")
def alembic_cfg() -> Any:
    return _alembic_config()


@pytest.fixture(scope="session")
def pg_scratch_engine(postgres_service: None) -> Iterator[sa.Engine]:
    """一次性 scratch 数据库上的 engine——绝不触碰 CONFIGS 配置的真实 dbname。

    两级供给：
    ① 配置的 PG 角色有 CREATEDB/superuser（CI service container 即此）→
       同实例建随机名 scratch 库，用毕 DROP；
    ② 否则 docker 可用 → 拉专用一次性 postgres 容器（与被测配置服务完全隔离，
       本机限权数据库的典型路径）；
    ③ 都不行 → skip（REQUIRE_SERVICES=1 时 fail，防 CI 静默跳过）。
    """
    admin_url = _pg_url(CONFIGS["postgresql"]["dbname"])
    if _pg_role_can_create_db(admin_url):
        scratch_name = f"multirag_test_{uuid.uuid4().hex[:12]}"
        with ExitStack() as resources:
            admin_engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
            resources.callback(admin_engine.dispose)

            def drop_scratch() -> None:
                with admin_engine.connect() as conn:
                    conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{scratch_name}" WITH (FORCE)'))

            resources.callback(drop_scratch)
            with admin_engine.connect() as conn:
                conn.execute(sa.text(f'CREATE DATABASE "{scratch_name}"'))
            engine = sa.create_engine(_pg_url(scratch_name))
            resources.callback(engine.dispose)
            with engine.begin() as conn:
                conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS usr_ai"))
            yield engine
        return

    if not _docker_usable():
        msg = "scratch 数据库无法供给：配置的 PG 角色无 CREATEDB 权限，且 docker/testcontainers 不可用"
        if os.environ.get("REQUIRE_SERVICES"):
            pytest.fail(msg, pytrace=False)
        pytest.skip(msg)

    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(services.service_images()["postgresql"], driver="psycopg", dbname=f"multirag_test_{uuid.uuid4().hex[:12]}")
    with ExitStack() as resources:
        resources.callback(container.stop)
        container.start()
        engine = sa.create_engine(container.get_connection_url())
        resources.callback(engine.dispose)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS usr_ai"))
        yield engine


@pytest.fixture(scope="session")
def bootstrapped_engine(pg_scratch_engine, alembic_cfg):
    """scratch 库上镜像生产全新环境引导：模型建表 + alembic stamp head。

    本仓库迁移链不自举空库（根迁移即 add_column，历史迁移只服务存量老库）；
    全新环境的权威引导是 api/db/db_models.py 的 ``init_database_tables()`` +
    ``upgrade_database_tables(is_fresh_install=True)``（直接 stamp head）。
    二者绑定模块级全局 engine 无法指向 scratch 库，此处用等价操作镜像。
    """
    from alembic import command

    from api.db.db_models import Base

    Base.metadata.create_all(pg_scratch_engine)
    with pg_scratch_engine.begin() as conn:
        cfg = _alembic_config()  # 独立实例，避免污染共享 alembic_cfg 的 attributes
        cfg.attributes["connection"] = conn
        command.stamp(cfg, "head")
    return pg_scratch_engine


@pytest.fixture
async def bootstrapped_async_engine(bootstrapped_engine):
    """bootstrapped_engine 的异步变体：同一 scratch 库、同一 psycopg3 驱动的 AsyncEngine。

    function 级（与 asyncio_default_fixture_loop_scope 匹配）：AsyncEngine 创建
    本身不建连接，按测试创建/释放零成本。
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(bootstrapped_engine.url)
    yield engine
    await engine.dispose()
