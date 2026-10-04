"""Owned, lazy test services with authenticated readiness and reversible config.

Only containers created here are stopped. Existing instances are probed read-only.
No application modules are imported until service endpoints have been resolved.
"""

import copy
import os
import time
from contextlib import ExitStack
from typing import Any

from tests.support.integration_suites import ROOT

ACTIVE_MANAGER: "ServiceManager | None" = None


class ServiceError(RuntimeError):
    """Credential-free readiness/provisioning failure safe for saved reports."""


def service_images() -> dict[str, str]:
    from ruamel.yaml import YAML

    with (ROOT / "docker/docker-compose-base.yml").open() as stream:
        services = YAML(typ="safe").load(stream)["services"]
    return {name: str(services[key]["image"]) for name, key in {"postgresql": "postgres", "redis": "redis", "minio": "minio", "milvus": "milvus-standalone", "infinity": "infinity"}.items()}


def split_address(value: str, default_port: int) -> tuple[str, int]:
    from urllib.parse import urlsplit

    parsed = urlsplit(value if "://" in value else f"//{value}")
    return parsed.hostname or "127.0.0.1", parsed.port or default_port


def probe(service: str, configs: dict[str, Any]) -> None:
    """Check protocol/authentication, not just an open TCP port. Do not write data."""
    conf = configs.get(service)
    if not conf:
        raise ValueError(f"Missing configuration section: {service}")
    if service == "postgresql":
        import sqlalchemy as sa

        url = sa.URL.create("postgresql+psycopg", username=conf["user"], password=str(conf["password"]), host=conf["host"], port=int(conf["port"]), database=conf["dbname"])
        engine = sa.create_engine(url, connect_args={"connect_timeout": 3})
        try:
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT 1")) == 1
        finally:
            engine.dispose()
    elif service == "redis":
        import redis

        host, port = split_address(conf["host"], 6379)
        with redis.Redis(
            host=host, port=port, db=int(conf.get("db", 1)), username=conf.get("username") or None, password=conf.get("password") or None, socket_connect_timeout=3, socket_timeout=3
        ) as client:
            assert client.ping()
    elif service == "minio":
        import urllib3
        from minio import Minio

        http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=3, read=3), retries=False)
        try:
            client = Minio(conf["host"], access_key=conf["user"], secret_key=str(conf["password"]), secure=str(conf.get("secure", False)).lower() in {"true", "1"}, http_client=http)
            client.list_buckets()
        finally:
            http.clear()
    elif service == "milvus":
        from pymilvus import MilvusClient

        client = MilvusClient(uri=conf["hosts"], user=conf.get("username", ""), password=conf.get("password", ""), db_name=conf.get("db_name") or "default", token=conf.get("token", ""), timeout=3)
        try:
            client.get_server_version(timeout=3)
        finally:
            client.close()
    elif service == "infinity":
        import subprocess
        import sys

        # Run the actual version handshake and read in a bounded subprocess:
        # the SDK has no connect/read timeout. An open TCP port is insufficient.
        host, port = split_address(os.environ.get("INFINITY_TEST_URI", conf["uri"]), 23817)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import infinity,sys; from infinity.common import NetworkAddress; c=infinity.connect(NetworkAddress(sys.argv[1],int(sys.argv[2]))); r=c.list_databases(); assert r.error_code == 0; c.disconnect()",
                host,
                str(port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if result.returncode:
            raise ConnectionError("Infinity protocol handshake failed")
    else:
        raise ValueError(f"Unknown test service: {service}")


class ServiceManager:
    def __init__(self, configs: dict[str, Any], *, allow_containers: bool = True) -> None:
        self.configs = configs
        self.allow_containers = allow_containers and os.environ.get("INTEGRATION_NO_TESTCONTAINERS", "").lower() not in {"1", "true", "yes"}
        self.stack = ExitStack()
        self.ready: dict[str, dict[str, Any]] = {}

    def __enter__(self) -> "ServiceManager":
        return self

    def __exit__(self, *args: Any) -> None:
        self.stack.close()

    def ensure(self, service: str) -> None:
        if service in self.ready:
            return
        started = time.monotonic()
        source = "existing"
        try:
            probe(service, self.configs)
        except Exception as exc:
            if not self.allow_containers or service == "milvus" or (service == "infinity" and os.environ.get("INFINITY_TEST_URI")):
                # Never include exception text: drivers often include credentials.
                raise ServiceError(f"Test service {service} is not ready ({type(exc).__name__}); provision it or enable Testcontainers") from None
            try:
                self._start(service)
            except Exception as error:
                raise ServiceError(f"Could not provision test service {service} ({type(error).__name__})") from None
            source = "testcontainers"
            deadline = time.monotonic() + 60
            while True:
                try:
                    probe(service, self.configs)
                    break
                except Exception as error:
                    if time.monotonic() >= deadline:
                        raise ServiceError(f"Test container {service} is not ready ({type(error).__name__})") from None
                    time.sleep(0.25)
        self.ready[service] = {"source": source, "seconds": round(time.monotonic() - started, 4)}

    def _start(self, service: str) -> None:
        images = service_images()
        saved = copy.deepcopy(self.configs.get(service))
        conf = dict(saved or {})
        container: Any
        if service == "postgresql":
            from testcontainers.postgres import PostgresContainer

            container = PostgresContainer(images[service], username="test", password="test", dbname="postgres", driver="psycopg")
            conf.update(name="postgresql", user="test", password="test", dbname="postgres")
            port = 5432
        elif service == "redis":
            from testcontainers.redis import RedisContainer

            container = RedisContainer(images[service])
            conf.update(username="", password="", db=1)
            port = 6379
        elif service == "minio":
            from testcontainers.minio import MinioContainer

            container = MinioContainer(images[service], access_key="minioadmin", secret_key="test-password")
            conf.update(user="minioadmin", password="test-password", secure=False)
            port = 9000
        elif service == "infinity":
            from testcontainers.core.container import DockerContainer

            container = DockerContainer(images[service]).with_exposed_ports(23817)
            port = 23817
        else:
            raise ValueError(f"No container adapter for {service}")
        # Register before start: an image/pull/readiness failure can leave a
        # partially created container. Testcontainers stop() handles this case.
        self.stack.callback(container.stop)
        container.start()
        host, mapped_port = container.get_container_host_ip(), int(container.get_exposed_port(port))
        if service == "postgresql":
            conf.update(host=host, port=mapped_port)
        elif service == "infinity":
            conf["uri"] = f"{host}:{mapped_port}"
        else:
            conf["host"] = f"{host}:{mapped_port}"
        self.stack.callback(self._restore, service, saved)
        self.configs[service] = conf

    def _restore(self, service: str, saved: dict[str, Any] | None) -> None:
        if saved is None:
            self.configs.pop(service, None)
        else:
            self.configs[service] = saved
