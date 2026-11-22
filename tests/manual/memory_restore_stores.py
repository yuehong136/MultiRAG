"""Owned PostgreSQL, Milvus, and Valkey backup and restore tools for the memory drill."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import tarfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any, BinaryIO, cast
from urllib.request import urlopen
from uuid import uuid4

import redis
import sqlalchemy as sa
import yaml
from minio import Minio
from pymilvus import MilvusClient

from tests.manual import memory_milvus_cold_volumes as cold
from tests.support.resource_journal import ResourceJournal

_ROOT = Path(__file__).resolve().parents[2]
_BASE_COMPOSE = _ROOT / "docker/docker-compose-milvus-rehearsal.yml"
_IMAGES = {"postgresql": "postgres:16-alpine", "redis": "valkey/valkey:8", "standalone": "milvusdb/milvus:v3.0.6"}
_PORTS = {"postgresql": (5432,), "redis": (6379,), "minio": (9000,), "standalone": (19530, 9091)}
_USER = "memory_restore"
_BUCKET = "a-bucket"
_BACKUP_PREFIX = "memory-restore-archive"
_BACKUP_VERSION = "0.6.2"
_BACKUP_SHA256 = {
    ("Darwin", "arm64"): "7ff0b460f445aa6e05e573467dc4f82696b8665869d77031f824adb825b37009",
    ("Darwin", "x86_64"): "6d04845e2db9fa81d728bd544159dc645887eeee6703a5972131ebb429ea51c2",
    ("Linux", "arm64"): "d8a071bcaa01d722b0a32958aaa2c588c3ad45cf45531c55546ecf8d3eb32324",
    ("Linux", "x86_64"): "b8d46d25020370c34ee7091f9747adf929bfdb19dfeffc0097c96a1f3e76dd1a",
}


def _private(path: Path, mode: str = "xb") -> IO[Any]:
    return open(path, mode, opener=lambda filename, flags: os.open(filename, flags, 0o600))


def _output(args: list[str]) -> str:
    return subprocess.check_output(args, text=True, timeout=15).strip()


def _wait(probe: Callable[[], bool], label: str, *, timeout: float = 150) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if probe():
                return
        except Exception as error:
            last_error = error
        time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for isolated {label}") from last_error


def _backup_program(work: Path, journal: ResourceJournal) -> Path:
    os_name, arch = platform.system(), platform.machine()
    if arch == "aarch64":
        arch = "arm64"
    digest = _BACKUP_SHA256.get((os_name, arch))
    if digest is None:
        raise ValueError(f"No pinned Milvus Backup release for {os_name}/{arch}")
    name = f"milvus-backup_{_BACKUP_VERSION}_{os_name}_{arch}.tar.gz"
    release_url = f"https://github.com/zilliztech/milvus-backup/releases/download/v{_BACKUP_VERSION}/{name}"
    archive_path = work / name
    binary = work / f"milvus-backup-{_BACKUP_VERSION}"
    if not archive_path.exists():
        journal.event("reserve-private-consumer-file", path=str(archive_path), url=release_url, expected_sha256=digest)
        with urlopen(release_url, timeout=30) as fetch, _private(archive_path) as output:
            shutil.copyfileobj(fetch, output)
    with archive_path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("Milvus Backup release checksum mismatch")
    if not binary.exists():
        journal.event("reserve-private-consumer-file", path=str(binary), release=f"v{_BACKUP_VERSION}")
        with tarfile.open(archive_path) as archive:
            members = [member for member in archive.getmembers() if PurePosixPath(member.name).name == "milvus-backup" and member.isfile()]
            if len(members) != 1:
                raise ValueError("Expected one Milvus Backup executable in the pinned release")
            payload = archive.extractfile(members[0])
            assert payload is not None
            with payload, _private(binary) as output:
                shutil.copyfileobj(payload, output)
        binary.chmod(0o700)
    version = subprocess.check_output([str(binary), "--help"], stderr=subprocess.STDOUT, text=True, timeout=15).splitlines()[0]
    if not version.startswith(_BACKUP_VERSION + " "):
        raise RuntimeError("Unexpected version banner in the Milvus Backup release")
    journal.event("milvus-backup-release", url=release_url, release_sha256=digest, version=version)
    return binary


def _resources(project: str) -> dict[str, list[str]]:
    label = f"label=com.docker.compose.project={project}"
    return {
        "containers": _output(["docker", "ps", "--all", "--quiet", "--filter", label]).splitlines(),
        "networks": _output(["docker", "network", "ls", "--quiet", "--filter", label]).splitlines(),
        "volumes": _output(["docker", "volume", "ls", "--quiet", "--filter", label]).splitlines(),
    }


def _object_prefix(member: tarfile.TarInfo) -> str:
    path = PurePosixPath(member.name)
    if not member.isfile() or len(path.parts) < 3 or path.parts[0] != _BACKUP_PREFIX or ".." in path.parts or path.as_posix() != member.name or not re.fullmatch(r"mr_[a-f0-9]{32}", path.parts[1]):
        raise ValueError("Invalid object in isolated Milvus backup archive")
    return path.parts[1]


@dataclass
class RestoreStores:
    """Connections and official backup commands restricted to one owned Docker project."""

    project: str
    config_path: Path
    ports: dict[int, int]
    database: str
    password: str = field(repr=False)
    backup_program: Path | None
    journal: ResourceJournal
    containers: dict[str, str] = field(default_factory=dict)

    @property
    def postgres_url(self) -> sa.engine.URL:
        return sa.engine.URL.create("postgresql+psycopg", username=_USER, password=self.password, host="127.0.0.1", port=self.ports[5432], database=self.database)

    @property
    def redis_url(self) -> str:
        return f"redis://127.0.0.1:{self.ports[6379]}/1"

    @property
    def milvus_uri(self) -> str:
        return f"http://127.0.0.1:{self.ports[19530]}"

    def overlay(self, base_configs: Mapping[str, Any]) -> dict[str, Any]:
        redis_config = {"host": f"127.0.0.1:{self.ports[6379]}", "db": 1, "username": "", "password": ""}
        return {
            **base_configs,
            "postgresql": {"name": "postgresql", "host": "127.0.0.1", "port": self.ports[5432], "dbname": self.database, "user": _USER, "password": self.password},
            "redis": redis_config,
            "datav_redis": redis_config,
            "milvus": {"hosts": self.milvus_uri, "db_name": "default", "username": "", "password": "", "token": ""},
            "minio": {"host": f"127.0.0.1:{self.ports[9000]}", "user": "minioadmin", "password": "minioadmin", "region": ""},
        }

    def _docker(self, *args: str) -> list[str]:
        return ["docker", "compose", "--project-name", self.project, "--file", str(self.config_path), *args]

    def _redis(self) -> redis.Redis:
        return redis.Redis.from_url(self.redis_url, socket_connect_timeout=3, socket_timeout=3)

    def _milvus(self) -> MilvusClient:
        return MilvusClient(uri=self.milvus_uri, timeout=5)

    def _minio(self) -> Minio:
        return Minio(f"127.0.0.1:{self.ports[9000]}", access_key="minioadmin", secret_key="minioadmin", secure=False)

    def _redis_ready(self) -> bool:
        with closing(self._redis()) as client:
            return client.ping() is True

    def _milvus_ready(self) -> bool:
        with urlopen(f"http://127.0.0.1:{self.ports[9091]}/healthz", timeout=2) as response:
            return response.status == 200 and self._minio().bucket_exists(_BUCKET)

    def assert_alive(self) -> None:
        for service, original in self.containers.items():
            current = _output(self._docker("ps", "--quiet", service))
            if not current or original != current:
                raise RuntimeError(f"Isolated {service} container changed or stopped")
        for service, container_ports in _PORTS.items():
            for source in container_ports:
                if _output(self._docker("port", service, str(source))) != f"127.0.0.1:{self.ports[source]}":
                    raise RuntimeError(f"Isolated {service} binding changed")

    def _postgres(self, tool: str, *args: str) -> list[str]:
        return self._docker("exec", "-T", "postgresql", tool, "--no-password", "--username", _USER, "--dbname", self.database, *args)

    def _valkey_persistence(self, client: redis.Redis) -> dict[str, Any]:
        config = client.config_get("appendonly", "dbfilename", "maxmemory-policy")
        if config != {"appendonly": "no", "dbfilename": "dump.rdb", "maxmemory-policy": "allkeys-lru"}:
            raise RuntimeError("Expected the isolated Valkey RDB configuration")
        return config

    def _backup_cli(self, *args: str) -> None:
        if self.backup_program is None:
            raise RuntimeError("Milvus Backup is unavailable in the physical restore drill")
        config = self.config_path.with_name(self.config_path.stem + "-milvus-backup.json")
        if not config.exists():
            self.journal.event("reserve-private-consumer-file", path=str(config))
            with _private(config, "x") as output:
                json.dump(
                    {
                        "configVersion": "v2",
                        "milvus": {
                            "grpc": {"address": "127.0.0.1", "port": self.ports[19530]},
                            "management": {"endpoint": f"http://127.0.0.1:{self.ports[9091]}"},
                            "storage": {
                                "provider": "minio",
                                "address": "127.0.0.1",
                                "port": self.ports[9000],
                                "useSSL": False,
                                "bucketName": _BUCKET,
                                "rootPath": "files",
                                "auth": {"type": "static", "accessKeyID": "minioadmin", "secretAccessKey": "minioadmin"},
                            },
                        },
                        "backup": {"storage": {"bucketName": _BUCKET, "rootPath": _BACKUP_PREFIX, "milvusAddress": "minio", "milvusPort": 9000}},
                        "transfer": {"mode": "auto"},
                    },
                    output,
                )
        subprocess.run([str(self.backup_program), *args, "--config", str(config)], cwd=self.config_path.parent, check=True, timeout=240)

    def _backup_milvus(self, archive_path: Path, collections: list[str]) -> None:
        with closing(self._milvus()) as milvus:
            available = set(milvus.list_collections())
            if not collections or set(collections) - available:
                raise ValueError("Backup requires live owned collections")
            for collection in collections:
                milvus.flush(collection_name=collection, timeout=60)
        name = f"mr_{uuid4().hex}"
        prefix = f"{_BACKUP_PREFIX}/{name}/"
        self.journal.event("restore-drill-reserve-object-prefix", project=self.project, bucket=_BUCKET, prefix=prefix)
        self._backup_cli("create", "--format", "snapshot", "--filter", ",".join(collections), "-n", name)
        minio = self._minio()
        objects = list(minio.list_objects(_BUCKET, prefix=prefix, recursive=True))
        keys = {obj.object_name or "" for obj in objects}
        if prefix + "meta/backup_meta.json" not in keys or not any(key.startswith(prefix + "bundle/") for key in keys):
            raise RuntimeError("Milvus Backup did not produce snapshot metadata and a bundle")
        with _private(archive_path) as payload, tarfile.open(fileobj=payload, mode="w") as archive:
            for obj in sorted(objects, key=lambda candidate: candidate.object_name or ""):
                assert obj.object_name is not None and obj.size is not None
                member = tarfile.TarInfo(obj.object_name)
                member.size = obj.size
                _object_prefix(member)
                response = minio.get_object(_BUCKET, obj.object_name)
                try:
                    archive.addfile(member, response)
                finally:
                    response.close()
                    response.release_conn()
        self.journal.event("restore-drill-milvus-archive", project=self.project, format="snapshot", object_count=len(objects), collections=collections)

    def _restore_milvus(self, archive_path: Path, collections: list[str]) -> None:
        minio = self._minio()
        with tarfile.open(archive_path) as archive:
            members = archive.getmembers()
            names = {_object_prefix(member) for member in members}
            if len(names) != 1 or len(members) != len({item.name for item in members}):
                raise ValueError("Expected exactly one Milvus backup with unique objects")
            name = names.pop()
            prefix = f"{_BACKUP_PREFIX}/{name}/"
            keys = {member.name for member in members}
            if prefix + "meta/backup_meta.json" not in keys or not any(key.startswith(prefix + "bundle/") for key in keys):
                raise ValueError("Missing Milvus snapshot metadata or bundle")
            self.journal.event("restore-drill-reserve-object-prefix", project=self.project, bucket=_BUCKET, prefix=prefix)
            for member in members:
                payload = archive.extractfile(member)
                assert payload is not None
                with payload:
                    minio.put_object(_BUCKET, member.name, cast(BinaryIO, payload), length=member.size)
        # In the target, both the copied bundle and the data use this project's MinIO.
        self._backup_cli("restore", "--strategy", "restore_snap", "--filter", ",".join(collections), "-n", name)
        with closing(self._milvus()) as milvus:
            if set(milvus.list_collections()) != set(collections):
                raise RuntimeError("Restored Milvus collection set differs from the checkpoint")
            for collection in collections:
                milvus.load_collection(collection, timeout=120)

    def _cold_containers(self) -> dict[str, str]:
        return {service: _output(self._docker("ps", "--all", "--quiet", service)) for service in cold.SERVICES}

    def _cold_profile(self) -> dict[str, object]:
        profile = cold.inspect_stack(self.project, self._cold_containers())
        # Image defaults and the selected Compose overrides jointly define
        # this exact topology. inspect_stack rejects any additional mounts.
        config = yaml.safe_load(_output(self._docker("exec", "-T", "standalone", "cat", "/milvus/configs/milvus.yaml")))
        etcd, minio, woodpecker = (config[key] for key in ("etcd", "minio", "woodpecker"))
        if minio["bucketName"] != _BUCKET or woodpecker["meta"]["type"] != "etcd" or woodpecker["storage"]["type"] != "minio":
            raise ValueError("Unexpected Milvus bucket or WAL topology")
        if not _output(self._docker("exec", "-T", "etcd", "etcdctl", "get", "--prefix", "--keys-only", etcd["rootPath"] + "/")):
            raise ValueError("The configured etcd prefix has no Milvus metadata")
        if not any(self._minio().list_objects(_BUCKET, prefix=minio["rootPath"] + "/", recursive=True)):
            raise ValueError("The configured bucket prefix has no Milvus objects")
        return {
            **profile,
            "etcd": {key: etcd[key] for key in ("rootPath", "metaSubPath", "kvSubPath")},
            "minio": {key: minio[key] for key in ("bucketName", "rootPath")},
            "wal": {"metadata": woodpecker["meta"]["type"], **woodpecker["storage"]},
        }

    def _stop_cold_milvus(self) -> None:
        self.assert_alive()
        for services in (("standalone",), ("minio", "etcd")):
            subprocess.run(self._docker("stop", "--timeout", "35", *services), check=True, timeout=55)
            for service in services:
                state = json.loads(_output(["docker", "inspect", "--format", "{{json .State}}", self.containers[service]]))
                if state["Status"] != "exited" or state["ExitCode"] != 0 or state["OOMKilled"]:
                    raise RuntimeError(f"Isolated {service} did not shut down cleanly, exit={state['ExitCode']}")
                self.journal.event("milvus-cold-service-stopped", project=self.project, service=service, exit_code=state["ExitCode"])

    def _start_cold_milvus(self) -> None:
        started = time.monotonic()
        subprocess.run(self._docker("start", "etcd", "minio"), check=True, timeout=30)

        def etcd_ready() -> bool:
            return "is healthy" in _output(self._docker("exec", "-T", "etcd", "etcdctl", "endpoint", "health"))

        _wait(etcd_ready, "cold etcd startup")
        _wait(lambda: self._minio().bucket_exists(_BUCKET), "cold MinIO bucket")
        subprocess.run(self._docker("start", "standalone"), check=True, timeout=30)
        _wait(self._milvus_ready, "cold Milvus startup")
        self.journal.event("milvus-cold-services-ready", project=self.project, seconds=time.monotonic() - started)

    def _backup_cold_milvus(self, path: Path, collections: list[str]) -> None:
        with closing(self._milvus()) as milvus:
            if set(collections) - set(milvus.list_collections()):
                raise ValueError("Cannot cold-archive a missing owned Milvus collection")
            for name in collections:
                milvus.flush(collection_name=name, timeout=60)
        profile = self._cold_profile()
        image = _output(["docker", "inspect", "--format", "{{.Image}}", self.containers["standalone"]])
        self._stop_cold_milvus()
        cold.archive_volumes(path, self.project, profile, image, self.journal)
        self._start_cold_milvus()
        if self._cold_profile() != profile:
            raise ValueError("Cold source profile changed after restart")

    def restart_cold_milvus(self) -> None:
        self._stop_cold_milvus()
        self._start_cold_milvus()
        self.assert_alive()

    def backup(self, destination: Path, collections: list[str]) -> dict[str, Path]:
        """Capture independent archives after our caller has fenced and drained writes."""
        self.assert_alive()
        if len(collections) != len(set(collections)) or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in collections):
            raise ValueError("Expected unique, exact collection selectors")
        files = {"postgresql": destination / "postgres.dump", "milvus": destination / "milvus.tar", "redis": destination / "valkey.rdb"}
        self.journal.event("reserve-private-consumer-file", path=str(destination))
        destination.mkdir(mode=0o700)
        for name, path in files.items():
            self.journal.event("reserve-private-consumer-file", path=str(path), component=name)
        with _private(files["postgresql"]) as archive:
            subprocess.run(self._postgres("pg_dump", "--format=custom", "--no-owner", "--no-acl"), stdout=archive, check=True, timeout=90)
        if self.backup_program is None:
            self._backup_cold_milvus(files["milvus"], collections)
        else:
            self._backup_milvus(files["milvus"], collections)
        with closing(self._redis()) as client:
            config = self._valkey_persistence(client)
            saves = cast(dict[str, Any], client.info("persistence"))["rdb_saves"]
            client.bgsave()

            def complete() -> bool:
                status = cast(dict[str, Any], client.info("persistence"))
                return status["rdb_saves"] > saves and not status["rdb_bgsave_in_progress"] and status["rdb_last_bgsave_status"] == "ok"

            _wait(complete, "Valkey RDB save", timeout=25)
            self.journal.event("restore-drill-valkey-persistence", project=self.project, configuration=config, last_save=cast(datetime, client.lastsave()).isoformat())
        subprocess.run(self._docker("cp", "redis:/data/dump.rdb", str(files["redis"])), check=True, timeout=25)
        files["redis"].chmod(0o600)
        for name, path in files.items():
            with path.open("rb") as content:
                self.journal.event(
                    "restore-drill-archive-written", project=self.project, component=name, path=str(path), size=path.stat().st_size, sha256=hashlib.file_digest(content, "sha256").hexdigest()
                )
        return files

    def restore(self, archives: Mapping[str, Path], collections: list[str]) -> None:
        """Load a validated checkpoint into three empty isolated targets."""
        self.assert_alive()
        if set(archives) != {"postgresql", "milvus", "redis"} or any(not path.is_file() or path.is_symlink() for path in archives.values()):
            raise ValueError("Restore requires a complete, externally validated archive set")
        with archives["postgresql"].open("rb") as pg, archives["redis"].open("rb") as valkey:
            if pg.read(5) != b"PGDMP" or valkey.read(5) != b"REDIS":
                raise ValueError("Unexpected PostgreSQL or Valkey archive format")
        if _output(self._postgres("psql", "-XAt", "--command", "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')")) != "0":
            raise ValueError("PostgreSQL restore target is not empty")
        with closing(self._milvus()) as milvus, closing(self._redis()) as client:
            expected_collections = set(collections) if self.backup_program is None else set()
            if set(milvus.list_collections()) != expected_collections or client.info("keyspace"):
                raise ValueError("Unexpected collections or keys in the restore target")
            self._valkey_persistence(client)
        with archives["postgresql"].open("rb") as source:
            subprocess.run(self._postgres("pg_restore", "--single-transaction", "--exit-on-error", "--no-owner", "--no-acl"), stdin=source, check=True, timeout=120)
        if self.backup_program is not None:
            self._restore_milvus(archives["milvus"], collections)
        subprocess.run(self._docker("stop", "--timeout", "10", "redis"), check=True, timeout=25)
        subprocess.run(self._docker("cp", str(archives["redis"]), "redis:/data/dump.rdb"), check=True, timeout=25)
        subprocess.run(self._docker("start", "redis"), check=True, timeout=25)
        _wait(self._redis_ready, "Valkey RDB restore")
        self.assert_alive()
        self.journal.event("restore-drill-artifacts-restored", project=self.project, collections=collections, valkey="RDB", postgresql="pg_restore --single-transaction")


@contextmanager
def isolated_stores(work: Path, journal: ResourceJournal, *, name: str, cold_milvus: bool = False, milvus_archive: Path | None = None) -> Iterator[RestoreStores]:
    """Start a disposable copy of every persisted service used by the drill."""
    if not os.environ.get("MULTIRAG_RUN_ID") or not os.environ.get("MULTIRAG_RUN_JOURNAL") or not re.fullmatch(r"[a-z][a-z0-9-]{0,28}", name):
        raise ValueError("Use a registered runner and an isolated stack name")
    if not work.resolve().is_relative_to(Path(os.environ["TMPDIR"]).resolve()):
        raise ValueError("Place private drill resources in the registered temporary directory")
    project = f"mr-restore-{uuid4().hex[:12]}-{name}"
    if any(_resources(project).values()):
        raise RuntimeError("Restore project already exists")
    password = secrets.token_hex(24)
    database = f"multirag_test_{uuid4().hex[:12]}"
    compose = yaml.safe_load(_BASE_COMPOSE.read_text())
    services = compose["services"]
    services["standalone"]["image"] = _IMAGES["standalone"]
    if milvus_archive is not None and not cold_milvus:
        raise ValueError("A physical archive requires the physical restore mode")
    if milvus_archive is not None and (milvus_archive.is_symlink() or not milvus_archive.resolve(strict=True).is_relative_to(work.resolve())):
        raise ValueError("Use an owned archive within the registered recovery directory")
    if cold_milvus:
        # MinIO itself declares /data even though this profile serves
        # /minio_data. A named mount keeps the full topology owned and sealed.
        compose["volumes"]["minio_image_data"] = None
        for service, mounts in cold.MOUNTS.items():
            services[service]["volumes"] = [{"type": "volume", "source": volume, "target": mount, "volume": {"nocopy": True}} for mount, volume in mounts.items()]
    services["postgresql"] = {
        "image": _IMAGES["postgresql"],
        "environment": {"POSTGRES_USER": _USER, "POSTGRES_PASSWORD": password, "POSTGRES_DB": database},
        "volumes": ["postgresql_data:/var/lib/postgresql/data"],
        "mem_limit": "256m",
    }
    services["redis"] = {
        "image": _IMAGES["redis"],
        "command": ["valkey-server", "--protected-mode", "no", "--appendonly", "no", "--save", "", "--maxmemory", "128mb", "--maxmemory-policy", "allkeys-lru"],
        "volumes": ["redis_data:/data"],
        "mem_limit": "192m",
    }
    compose["volumes"].update(postgresql_data=None, redis_data=None)
    for settings in services.values():
        settings["pull_policy"] = "never"
    images = {service: _output(["docker", "image", "inspect", "--format", "{{.Id}}", settings["image"]]) for service, settings in services.items()}
    config_path = work / f"{project}.json"
    program = None if cold_milvus else _backup_program(work, journal)

    with ExitStack() as stack:
        ports: dict[int, int] = {}
        for service, container_ports in _PORTS.items():
            endpoints = []
            for container_port in container_ports:
                listener = stack.enter_context(socket.socket())
                listener.bind(("127.0.0.1", 0))
                host_port = listener.getsockname()[1]
                ports[container_port] = host_port
                endpoints.append(f"127.0.0.1:{host_port}:{container_port}")
            services[service]["ports"] = endpoints
        journal.event(
            "restore-drill-reserve-docker-project",
            project=project,
            database=database,
            loopback_ports=sorted(ports.values()),
            volumes=[f"{project}_{volume}" for volume in compose["volumes"]],
            services=list(services),
            network=f"{project}_default",
            image_ids=images,
        )
        journal.event("reserve-private-consumer-file", path=str(config_path))
        with _private(config_path, "x") as output:
            json.dump(compose, output)

    stores = RestoreStores(project, config_path, ports, database, password, program, journal)
    try:
        expected_services = ["postgresql", "redis", *cold.SERVICES]
        if milvus_archive is None:
            subprocess.run(stores._docker("run", "--rm", "--no-deps", "volume-init"), check=True, timeout=80)
            subprocess.run(stores._docker("up", "--detach", "--pull", "never", *expected_services), check=True, timeout=100)
        else:
            started = time.monotonic()
            subprocess.run(stores._docker("create", "--pull", "never", *cold.SERVICES), check=True, timeout=60)
            profile = cold.inspect_stack(project, stores._cold_containers())
            captured = cold.restore_volumes(milvus_archive, project, profile, images["standalone"], journal)
            journal.event("milvus-cold-restore-archives-applied", project=project, seconds=time.monotonic() - started)
            stores._start_cold_milvus()
            if stores._cold_profile() != captured:
                raise ValueError("Running cold restore profile differs from the archive")
            subprocess.run(stores._docker("up", "--detach", "--pull", "never", "postgresql", "redis"), check=True, timeout=80)
        stores.containers.update((service, _output(stores._docker("ps", "--quiet", service))) for service in expected_services)
        for service, container in stores.containers.items():
            if _output(["docker", "inspect", "--format", "{{.Image}}", container]) != images[service]:
                raise RuntimeError(f"Isolated {service} image changed")
        stores.assert_alive()
        postgres_probe = stores._postgres("psql", "-XAt", "--command", "SELECT current_database()")

        def postgres_ready() -> bool:
            return _output(postgres_probe) == database

        _wait(postgres_ready, "PostgreSQL startup")
        _wait(stores._redis_ready, "Valkey startup")
        _wait(stores._milvus_ready, "Milvus startup")
        with closing(stores._milvus()) as milvus, closing(stores._redis()) as client:
            if (milvus_archive is None and milvus.list_collections()) or client.info("keyspace"):
                raise RuntimeError("A new isolated store unexpectedly contains data")
            redis_mode = stores._valkey_persistence(client)
            journal.event(
                "restore-drill-docker-ready",
                project=project,
                database=database,
                containers=stores.containers,
                postgres_version=_output(stores._docker("exec", "-T", "postgresql", "pg_dump", "--version")),
                milvus_version=milvus.get_server_version(),
                valkey_version=cast(dict[str, Any], client.info("server")).get("valkey_version"),
                valkey_persistence=redis_mode,
            )
        yield stores
    finally:
        command = stores._docker("down", "--timeout", "15", "--volumes")
        subprocess.run(command, check=True, timeout=100)
        remaining = _resources(project)
        journal.event("restore-drill-docker-cleanup", project=project, remaining=remaining)
        if any(remaining.values()):
            raise RuntimeError("Isolated store cleanup left Docker resources")
