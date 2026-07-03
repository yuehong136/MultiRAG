"""tests/integration 共享设置。

本目录的测试需要真实基础服务（PostgreSQL / Redis / MinIO）：
- 服务不可达时整个目录自动 skip（本地随手跑 `make test-all` 不会误报红）
- 设置 REQUIRE_SERVICES=1 时改为硬失败（CI / `make integration` 用，防止静默跳过）

所有用例自动带上 `integration` marker，无需逐个标注。
"""

import os
import socket

import pytest

from common.config_utils import CONFIGS


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


def pytest_collection_modifyitems(config, items):
    for item in items:
        item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session", autouse=True)
def _require_services():
    missing = _missing_services()
    if not missing:
        return
    msg = f"基础服务不可达：{', '.join(missing)}（启动：docker compose -f docker/docker-compose-base.yml up -d）"
    if os.environ.get("REQUIRE_SERVICES"):
        pytest.fail(msg, pytrace=False)
    pytest.skip(msg)
