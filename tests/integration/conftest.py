"""tests/integration 共享设置。

三级探测（internal/test_form_upgrade_plan.md Phase C）：
① 服务已在运行（本机 compose / CI service containers）→ 直接用，行为与升级前一致；
② docker 可用且未设 ``INTEGRATION_NO_TESTCONTAINERS=1`` → session 级为缺失的服务
   拉起 testcontainers 容器（镜像与 docker/docker-compose-base.yml 钉住一致），
   并原地改写 ``CONFIGS`` 对应 section，测试无感；
③ 都不可用 → 整目录 skip；``REQUIRE_SERVICES=1`` 时硬失败（CI 语义不变）。

真库行为测试共享 fixtures：
- ``pg_scratch_engine``：一次性 scratch 数据库（session 末 DROP），
  **绝不触碰 CONFIGS 配置的真实 dbname**；
- ``bootstrapped_engine``：scratch 库上镜像生产 fresh-install 引导
  （模型建表 + alembic stamp head）；
- ``alembic_cfg``：cwd 无关的 alembic 配置。

所有用例自动带上 ``integration`` marker，无需逐个标注。
"""

import os
import socket
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from common.config_utils import CONFIGS

_REPO_ROOT = Path(__file__).resolve().parents[2]

# 镜像版本与 docker/docker-compose-base.yml、CI service containers 钉住一致
_POSTGRES_IMAGE = "postgres:16-alpine"
_VALKEY_IMAGE = "valkey/valkey:8"
_MINIO_IMAGE = "minio/minio:RELEASE.2024-12-18T13-15-44Z"


def _tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _split_host_port(value: str, default_port: int) -> tuple[str, int]:
    if ":" in value:
        host, _, port = value.rpartition(":")
        return host, int(port)
    return value, default_port


def _missing_services() -> list[str]:
    missing = []
    pg = CONFIGS.get("postgresql") or {}
    if pg and not _tcp_ok(pg.get("host", "127.0.0.1"), int(pg.get("port", 5432))):
        missing.append("postgresql")
    redis_conf = CONFIGS.get("redis") or {}
    if redis_conf and not _tcp_ok(*_split_host_port(redis_conf.get("host", "127.0.0.1:6379"), 6379)):
        missing.append("redis")
    minio_conf = CONFIGS.get("minio") or {}
    if minio_conf and not _tcp_ok(*_split_host_port(minio_conf.get("host", "127.0.0.1:9000"), 9000)):
        missing.append("minio")
    return missing


def _docker_usable() -> bool:
    """探测级②的准入：显式关闭开关 > docker daemon 可达性。"""
    if os.environ.get("INTEGRATION_NO_TESTCONTAINERS"):
        return False
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


def _start_container_for(service: str) -> Any:
    """为缺失服务拉起容器并原地改写 CONFIGS 对应 section，返回容器句柄。"""
    if service == "postgresql":
        from testcontainers.postgres import PostgresContainer

        pg_conf = dict(CONFIGS.get("postgresql") or {})
        container = PostgresContainer(
            _POSTGRES_IMAGE,
            username=pg_conf.get("user", "usr_ai"),
            password=str(pg_conf.get("password", "123456")),
            dbname=pg_conf.get("dbname", "postgres"),
            driver="psycopg",
        )
        container.start()
        engine = sa.create_engine(container.get_connection_url())
        try:
            with engine.begin() as conn:  # 镜像 docker/postgres/init.sql
                conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS usr_ai"))
        finally:
            engine.dispose()
        CONFIGS["postgresql"] = {
            **pg_conf,
            "name": "postgresql",
            "host": container.get_container_host_ip(),
            "port": int(container.get_exposed_port(5432)),
        }
        return container

    if service == "redis":
        from testcontainers.redis import RedisContainer

        redis_conf = dict(CONFIGS.get("redis") or {})
        container = RedisContainer(_VALKEY_IMAGE)
        container.start()
        CONFIGS["redis"] = {
            **redis_conf,
            "username": "",
            "password": "",
            "host": f"{container.get_container_host_ip()}:{container.get_exposed_port(6379)}",
        }
        return container

    if service == "minio":
        from testcontainers.minio import MinioContainer

        minio_conf = dict(CONFIGS.get("minio") or {})
        container = MinioContainer(
            _MINIO_IMAGE,
            access_key=minio_conf.get("user", "minioadmin"),
            secret_key=str(minio_conf.get("password", "12345678")),
        )
        container.start()
        CONFIGS["minio"] = {
            **minio_conf,
            "user": container.access_key,
            "password": container.secret_key,
            "host": f"{container.get_container_host_ip()}:{container.get_exposed_port(9000)}",
        }
        return container

    raise ValueError(f"未知服务: {service}")


def pytest_collection_modifyitems(config, items):
    for item in items:
        item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session", autouse=True)
def _require_services():
    missing = _missing_services()
    containers: list[Any] = []
    start_error = ""
    if missing and _docker_usable():
        try:
            for service in missing:
                containers.append(_start_container_for(service))
            missing = _missing_services()  # 容器就绪后复检
        except Exception as exc:  # 拉起失败（如离线拉不到镜像）→ 降级为 skip 而非报错
            start_error = f"；testcontainers 拉起失败: {exc!r}"
            for container in containers:
                try:
                    container.stop()
                except Exception:
                    pass
            containers = []
    try:
        if missing:
            msg = f"基础服务不可达：{', '.join(missing)}（启动：docker compose -f docker/docker-compose-base.yml up -d；或保持 docker 可用交给 testcontainers 自动拉起）{start_error}"
            if os.environ.get("REQUIRE_SERVICES"):
                pytest.fail(msg, pytrace=False)
            pytest.skip(msg)
        yield
    finally:
        for container in containers:
            try:
                container.stop()
            except Exception:
                pass


def _alembic_config() -> Any:
    """cwd 无关的 alembic 配置（script_location 锚定仓库根）。"""
    from alembic.config import Config

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "configs" / "alembic"))
    return cfg


@pytest.fixture(scope="session")
def alembic_cfg() -> Any:
    return _alembic_config()


def _pg_url(dbname: str) -> sa.engine.URL:
    pg = CONFIGS["postgresql"]
    return sa.engine.URL.create(
        "postgresql+psycopg",
        username=pg["user"],
        password=str(pg["password"]),
        host=pg["host"],
        port=int(pg["port"]),
        database=dbname,
    )


def _pg_role_can_create_db(url: sa.engine.URL) -> bool:
    """只读探测：配置的 PG 角色是否有 CREATEDB/superuser 权限。"""
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            return bool(conn.execute(sa.text("SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user")).scalar())
    except Exception:
        return False
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def pg_scratch_engine(_require_services):
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
        admin_engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{scratch_name}"'))
        engine = sa.create_engine(_pg_url(scratch_name))
        try:
            with engine.begin() as conn:
                conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS usr_ai"))
            yield engine
        finally:
            engine.dispose()
            with admin_engine.connect() as conn:
                conn.execute(sa.text(f'DROP DATABASE "{scratch_name}" WITH (FORCE)'))
            admin_engine.dispose()
        return

    if not _docker_usable():
        msg = "scratch 数据库无法供给：配置的 PG 角色无 CREATEDB 权限，且 docker/testcontainers 不可用"
        if os.environ.get("REQUIRE_SERVICES"):
            pytest.fail(msg, pytrace=False)
        pytest.skip(msg)

    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(_POSTGRES_IMAGE, driver="psycopg")
    container.start()
    engine = sa.create_engine(container.get_connection_url())
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS usr_ai"))
        yield engine
    finally:
        engine.dispose()
        container.stop()


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
