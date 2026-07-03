"""探测本地基础服务（PostgreSQL / Redis / MinIO）是否可达。

供 `make integration` 前置检查使用：全部可达时静默退出 0；
否则列出缺失的服务并退出 1。

地址从 configs/service_conf.yaml 读取（configs/local.service_conf.yaml
的同名顶层 section 会整体覆盖，与运行时行为一致）。
"""

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 允许以脚本方式直接运行


def _tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _split_host_port(value: str, default_port: int) -> tuple[str, int]:
    """解析 'host:port' 形式的配置值（redis/minio 的 host 字段带端口）。"""
    if ":" in value:
        host, _, port = value.rpartition(":")
        return host, int(port)
    return value, default_port


def main() -> int:
    from common.config_utils import CONFIGS

    targets: dict[str, tuple[str, int]] = {}

    pg = CONFIGS.get("postgresql") or {}
    if pg:
        targets["postgresql"] = (pg.get("host", "127.0.0.1"), int(pg.get("port", 5432)))

    redis_conf = CONFIGS.get("redis") or {}
    if redis_conf:
        targets["redis"] = _split_host_port(redis_conf.get("host", "127.0.0.1:6379"), 6379)

    minio_conf = CONFIGS.get("minio") or {}
    if minio_conf:
        targets["minio"] = _split_host_port(minio_conf.get("host", "127.0.0.1:9000"), 9000)

    missing = []
    for name, (host, port) in targets.items():
        if not _tcp_ok(host, port):
            missing.append(f"  - {name}: {host}:{port} 不可达")

    if missing:
        print("以下基础服务未就绪：", file=sys.stderr)
        print("\n".join(missing), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
