"""基础服务连通性集成测试。

验证按 configs/service_conf.yaml（含 local 覆盖）能真实连通
PostgreSQL / Redis / MinIO。服务缺失时由 conftest 整体 skip；
CI 中 REQUIRE_SERVICES=1 时硬失败。
"""

import sqlalchemy as sa

from common.config_utils import CONFIGS


def test_postgres_select_one():
    pg = CONFIGS["postgresql"]
    url = f"postgresql+psycopg2://{pg['user']}:{pg['password']}@{pg['host']}:{pg['port']}/{pg['dbname']}"
    engine = sa.create_engine(url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT 1")).scalar() == 1
    finally:
        engine.dispose()


def test_redis_ping():
    import redis

    conf = CONFIGS["redis"]
    host, _, port = conf["host"].rpartition(":")
    client = redis.Redis(
        host=host,
        port=int(port),
        db=int(conf.get("db", 1)),
        username=conf.get("username") or None,
        password=conf.get("password") or None,
        socket_connect_timeout=3,
    )
    assert client.ping()


def test_minio_reachable():
    from minio import Minio

    conf = CONFIGS["minio"]
    client = Minio(conf["host"], access_key=conf["user"], secret_key=conf["password"], secure=False)
    client.list_buckets()  # 认证+连通即可，不要求已有 bucket
