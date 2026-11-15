"""Own isolated services for Go acceptance; credentials stay in private files."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("action", choices=["start", "stop"])
parser.add_argument("--root", type=Path, required=True)
args = parser.parse_args()
root = args.root.resolve()
root.mkdir(parents=True, exist_ok=True)
directory = root / "isolated-services"
reservation = root / "isolated-services-reservation.json"
if args.action == "stop":
    if not reservation.exists():
        raise SystemExit(0)
    spec = json.loads(reservation.read_text())
    extra = root / "isolated-infinity-reservation.json"
    if extra.exists():
        addition = json.loads(extra.read_text())
        for field in ("containers", "volumes", "ports", "images"):
            spec[field].update(addition[field])
    private_path = directory / "credentials.json"
    private = json.loads(private_path.read_text()) if private_path.exists() else {}
    removed = []
    for name in spec["containers"].values():
        result = subprocess.run(["docker", "inspect", name], capture_output=True, text=True)
        if result.returncode == 0:
            container = json.loads(result.stdout)[0]
            if container["Config"]["Labels"].get("owner") != "go-full-alignment" or container["Config"]["Labels"].get("run") != spec["run_id"]:
                raise RuntimeError("Refuse cleanup of unowned service")
            identity = container["Id"]
            logs = subprocess.run(["docker", "logs", identity], capture_output=True, text=True)
            output = logs.stdout + logs.stderr
            for secret in private.values():
                output = output.replace(secret, "[REDACTED]")
            (directory / (name + ".log")).write_text(output)
            subprocess.run(["docker", "rm", "-f", identity], check=True, stdout=subprocess.DEVNULL)
        inventory = subprocess.check_output(["docker", "container", "ls", "-a", "--format", "{{.Names}}"], text=True).splitlines()
        if name in inventory:
            raise RuntimeError("Owned service survived cleanup")
        removed.append(name)
    for name in spec["volumes"].values():
        result = subprocess.run(["docker", "volume", "inspect", name], capture_output=True, text=True)
        if result.returncode == 0:
            volume = json.loads(result.stdout)[0]
            if volume["Labels"].get("owner") != "go-full-alignment":
                raise RuntimeError("Refuse cleanup of unowned volume")
            subprocess.run(["docker", "volume", "rm", name], check=True, stdout=subprocess.DEVNULL)
        inventory = subprocess.check_output(["docker", "volume", "ls", "--format", "{{.Name}}"], text=True).splitlines()
        if name in inventory:
            raise RuntimeError("Owned volume survived cleanup")
    for port in spec["ports"].values():
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError("Owned listener survived cleanup")
    for name in ["credentials.json", "scratch.json", "service_conf.yaml"]:
        (directory / name).unlink(missing_ok=True)
    (root / "isolated-services-cleanup.json").write_text(
        json.dumps({"containers_absent": removed, "volumes_absent": list(spec["volumes"].values()), "ports_closed": list(spec["ports"].values()), "private_files_removed": True}, indent=2) + "\n"
    )
    print("Isolated Go services removed and independently checked")
    raise SystemExit(0)
if reservation.exists():
    raise SystemExit("Service reservation already exists; inspect before retry")
run = uuid.uuid4().hex[:10]
prefix = "multirag-go-align-" + run
ports = {}
for name in ("mysql", "redis", "nats", "nats_monitor", "minio", "es", "api", "admin", "infinity", "infinity_http"):
    while True:
        port = 22000 + secrets.randbelow(8000)
        if port in ports.values():
            continue
        with socket.socket() as sock:
            sock.settimeout(0.1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                continue
        ports[name] = port
        break
images = {
    "mysql": "mysql:8.0.39",
    "redis": "valkey/valkey:8",
    "nats": "nats:2.14.1",
    "minio": "minio/minio:RELEASE.2024-12-18T13-15-44Z",
    "es": "elasticsearch:8.11.3",
    "infinity": "infiniflow/infinity:v0.7.0",
}
spec = {
    "owner": "go-full-alignment",
    "run_id": run,
    "prefix": prefix,
    "directory": str(directory),
    "ports": ports,
    "containers": {name: prefix + "-" + name for name in images},
    "volumes": {name: prefix + "-" + name + "-data" for name in images},
    "images": images,
    "api_database": "multirag_go_align_api_" + run,
}
with reservation.open("x") as stream:
    json.dump(spec, stream, indent=2)
    stream.flush()
    os.fsync(stream.fileno())
directory.mkdir(mode=0o700)
infinity_config = directory / "infinity.toml"
source = Path("docker/infinity_conf.toml").read_text()
source = (
    source.replace('buffer_manager_size      = "8GB"', 'buffer_manager_size      = "512MB"')
    .replace('memindex_memory_quota    = "1GB"', 'memindex_memory_quota    = "256MB"')
    .replace("connection_pool_size     = 128", "connection_pool_size     = 32")
)
infinity_config.write_text(source)
mysql_password, minio_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
minio_user = "goalign" + run
private = {"mysql_password": mysql_password, "minio_user": minio_user, "minio_password": minio_password}
credentials = directory / "credentials.json"
credentials.write_text(json.dumps(private))
credentials.chmod(0o600)
environment = os.environ.copy()
environment.update({"MYSQL_ROOT_PASSWORD": mysql_password, "MYSQL_PWD": mysql_password, "MINIO_ROOT_USER": minio_user, "MINIO_ROOT_PASSWORD": minio_password})
events = directory / "events.jsonl"


def event(value: dict[str, object]) -> None:
    with events.open("a") as stream:
        stream.write(json.dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def command(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    event({"event": "command-intent", "command": arguments})
    process = subprocess.run(arguments, env=environment, capture_output=True, text=True)
    output = process.stdout + process.stderr
    for secret in private.values():
        output = output.replace(secret, "[REDACTED]")
    event({"event": "command-exit", "command": arguments, "raw_exit": process.returncode, "output": output[-6000:]})
    if check and process.returncode:
        raise RuntimeError("Container command failed; inspect private redacted events")
    return process


for name in images:
    command(["docker", "volume", "create", "--label", "owner=go-full-alignment", spec["volumes"][name]])
    arguments = ["docker", "run", "--rm", "-d", "--name", spec["containers"][name], "--label", "owner=go-full-alignment", "--label", "run=" + run]
    internal = {"mysql": 3306, "redis": 6379, "nats": 4222, "minio": 9000, "es": 9200, "infinity": 23817}[name]
    arguments += ["-p", "127.0.0.1:" + str(ports[name]) + ":" + str(internal)]
    if name == "nats":
        arguments += ["-p", "127.0.0.1:" + str(ports["nats_monitor"]) + ":8222"]
    mount = {"mysql": "/var/lib/mysql", "redis": "/data", "nats": "/data", "minio": "/data", "es": "/usr/share/elasticsearch/data", "infinity": "/var/infinity"}[name]
    arguments += ["-v", spec["volumes"][name] + ":" + mount]
    if name == "infinity":
        arguments += ["-p", "127.0.0.1:" + str(ports["infinity_http"]) + ":23820", "--ulimit", "nofile=500000:500000", "-v", str(infinity_config) + ":/infinity_conf.toml:ro"]
    if name == "mysql":
        arguments += ["-e", "MYSQL_ROOT_PASSWORD", "-e", "MYSQL_ROOT_HOST=%"]
    elif name == "minio":
        arguments += ["-e", "MINIO_ROOT_USER", "-e", "MINIO_ROOT_PASSWORD"]
    elif name == "es":
        arguments += ["-e", "discovery.type=single-node", "-e", "xpack.security.enabled=false", "-e", "ES_JAVA_OPTS=-Xms512m -Xmx512m"]
    arguments += [images[name]]
    if name == "redis":
        arguments += ["valkey-server", "--save", "", "--appendonly", "no", "--databases", "1024"]
    elif name == "nats":
        arguments += ["-js", "-sd", "/data", "-m", "8222"]
    elif name == "minio":
        arguments += ["server", "/data"]
    elif name == "infinity":
        arguments += ["-f", "/infinity_conf.toml"]
    result = command(arguments)
    event({"event": "container-created", "name": spec["containers"][name], "id": result.stdout.strip()})
deadline = time.monotonic() + 240
pending = set(images)
while pending and time.monotonic() < deadline:
    for name in list(pending):
        ready = False
        if name == "mysql":
            result = command(["docker", "exec", "-e", "MYSQL_PWD", spec["containers"][name], "mysql", "-uroot", "-N", "-e", "SELECT 1"], check=False)
            ready = result.returncode == 0 and result.stdout.strip() == "1"
        elif name == "redis":
            result = command(["docker", "exec", spec["containers"][name], "valkey-cli", "ping"], check=False)
            ready = result.returncode == 0 and result.stdout.strip() == "PONG"
        else:
            port = ports["nats_monitor"] if name == "nats" else ports["infinity_http"] if name == "infinity" else ports[name]
            route = {"nats": "/healthz", "minio": "/minio/health/live", "es": "/_cluster/health?wait_for_status=yellow&timeout=1s", "infinity": "/admin/node/current"}[name]
            try:
                with urllib.request.urlopen("http://127.0.0.1:" + str(port) + route, timeout=2) as response:
                    body = response.read()
                    ready = response.status == 200 and (name != "es" or json.loads(body)["status"] in ("yellow", "green"))
            except (OSError, ValueError):
                pass
        if ready:
            event({"event": "service-ready", "service": name})
            pending.remove(name)
    if pending:
        time.sleep(1)
if pending:
    raise RuntimeError("Isolated services did not become ready: " + ",".join(sorted(pending)))
command(["docker", "exec", "-e", "MYSQL_PWD", spec["containers"]["mysql"], "mysql", "-uroot", "-e", "CREATE DATABASE `" + spec["api_database"] + "`"])
scratch = {
    "Database": {"Driver": "mysql", "Host": "127.0.0.1", "Port": ports["mysql"], "Database": "mysql", "Username": "root", "Password": mysql_password, "Charset": "utf8mb4"},
    "Redis": {"Host": "127.0.0.1", "Port": ports["redis"], "DB": 1, "Password": ""},
    "MinIO": {"Host": "127.0.0.1:" + str(ports["minio"]), "User": minio_user, "Password": minio_password, "Secure": False, "Verify": True, "Region": "us-east-1"},
    "Elasticsearch": {"Hosts": "http://127.0.0.1:" + str(ports["es"])},
    "Nats": {"Host": "127.0.0.1", "Port": ports["nats"]},
    "Infinity": {"URI": "127.0.0.1:" + str(ports["infinity"]), "DBName": "default_db"},
}
path = directory / "scratch.json"
path.write_text(json.dumps(scratch, indent=2))
path.chmod(0o600)
configuration = {
    "server": {"mode": "release", "port": ports["api"], "secret_key": secrets.token_urlsafe(32)},
    "admin": {"host": "127.0.0.1", "http_port": ports["admin"] - 2},
    "mysql": {"host": "127.0.0.1", "port": ports["mysql"], "name": spec["api_database"], "user": "root", "password": mysql_password},
    "redis": {"host": "127.0.0.1:" + str(ports["redis"]), "db": 0, "password": ""},
    "nats": {"host": "127.0.0.1", "port": ports["nats"]},
    "task_executor": {"message_queue_type": "nats"},
    "doc_engine": {"type": "elasticsearch"},
    "es": {"hosts": "http://127.0.0.1:" + str(ports["es"]), "username": "", "password": ""},
    "minio": {"host": "127.0.0.1:" + str(ports["minio"]), "user": minio_user, "password": minio_password, "secure": False, "verify": True, "region": "us-east-1", "bucket": "", "prefix_path": ""},
    "authentication": {"enable_register": True, "disable_password_login": False},
    "log": {"level": "error"},
}
path = directory / "service_conf.yaml"
path.write_text(json.dumps(configuration, indent=2))
path.chmod(0o600)
event({"event": "reserve-redis-leases", "directory": str(directory / "redis-leases"), "first": 16, "last": 1023})
(directory / "redis-leases").mkdir(mode=0o700)
event({"event": "isolated-services-ready", "config": str(directory / "scratch.json"), "service_config": str(path)})
print(json.dumps({"event": "isolated-services-ready", "reservation": str(reservation), "scratch_config": str(directory / "scratch.json"), "service_config": str(path), "ports": ports}))
