"""Cold-archive the named volumes in the isolated Memory restore stack."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import IO, BinaryIO, cast
from uuid import uuid4

from tests.support.resource_journal import ResourceJournal

MOUNTS = {"etcd": {"/etcd": "etcd_data"}, "minio": {"/data": "minio_image_data", "/minio_data": "minio_data"}, "standalone": {"/var/lib/milvus": "milvus_data"}}
VOLUMES = tuple(volume for mounts in MOUNTS.values() for volume in mounts.values())
SERVICES = tuple(MOUNTS)
_MANIFEST = "milvus-cold.json"
_ENV = {
    "etcd": ("ETCD_AUTO_COMPACTION_MODE", "ETCD_AUTO_COMPACTION_RETENTION", "ETCD_QUOTA_BACKEND_BYTES", "ETCD_SNAPSHOT_COUNT"),
    "minio": (),
    "standalone": ("ETCD_ENDPOINTS", "MINIO_ADDRESS", "MQ_TYPE"),
}


def _inspect(*args: str) -> dict[str, object]:
    return json.loads(subprocess.check_output(["docker", *args], text=True, timeout=15))[0]


def _check_owned_volume(project: str, volume: str) -> None:
    metadata = _inspect("volume", "inspect", f"{project}_{volume}")
    labels = cast(dict[str, str], metadata["Labels"])
    if metadata["Driver"] != "local" or labels.get("com.docker.compose.project") != project or labels.get("com.docker.compose.volume") != volume:
        raise ValueError("Unowned or non-local cold stack volume")


def inspect_stack(project: str, containers: Mapping[str, str]) -> dict[str, object]:
    """Bind the archive to exact images, service configuration, and owned mounts."""
    if not project.startswith("mr-restore-") or set(containers) != set(SERVICES):
        raise ValueError("Expected the registered isolated Milvus services")
    profile = {}
    for service, identity in containers.items():
        metadata = _inspect("container", "inspect", identity)
        config = cast(dict[str, object], metadata["Config"])
        labels = cast(dict[str, str], config["Labels"])
        if labels.get("com.docker.compose.project") != project or labels.get("com.docker.compose.service") != service:
            raise ValueError("Unexpected cold stack container identity")
        mounts = cast(list[dict[str, object]], metadata["Mounts"])
        expected = MOUNTS[service]
        if len(mounts) != len(expected) or any(
            item["Type"] != "volume" or item["Destination"] not in expected or item["Name"] != f"{project}_{expected[str(item['Destination'])]}" or not item["RW"] for item in mounts
        ):
            raise ValueError(f"Unexpected persistent mount layout for {service}")
        for volume in expected.values():
            _check_owned_volume(project, volume)
        env = dict(item.split("=", 1) for item in cast(list[str], config["Env"]))
        profile[service] = {"image_digest": metadata["Image"], "command": config["Cmd"], "user": config.get("User", ""), "environment": {key: env[key] for key in _ENV[service]}, "mounts": expected}
    return {"services": profile}


def validate_volume_tar(payload: IO[bytes]) -> int:
    """Check exact member paths and types before the archive reaches Docker."""
    with tarfile.open(fileobj=payload, mode="r:") as archive:
        members: dict[PurePosixPath, bool] = {}
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or path in members or not (member.isfile() or member.isdir()):
                raise ValueError("Unsupported member in isolated volume archive")
            members[path] = member.isdir()
        if members.get(PurePosixPath(".")) is not True or any(not all(members.get(parent) for parent in path.parents) for path in members):
            raise ValueError("Incomplete directory tree in isolated volume archive")
    payload.seek(0)
    return len(members)


def validate_bundle(bundle: tarfile.TarFile) -> dict[str, object]:
    expected = {_MANIFEST, *(f"{volume}.tar" for volume in VOLUMES)}
    members = bundle.getmembers()
    if {member.name for member in members} != expected or len(members) != len(expected) or any(not member.isfile() for member in members):
        raise ValueError("Cold Milvus archive volume set incomplete or invalid")
    config = bundle.extractfile(_MANIFEST)
    assert config is not None
    with config:
        manifest = json.load(config)
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"format", "profile", "volumes"}
        or manifest["format"] != 1
        or not isinstance(manifest["profile"], dict)
        or not isinstance(manifest["volumes"], dict)
        or set(manifest["volumes"]) != set(VOLUMES)
    ):
        raise ValueError("Cold Milvus archive manifest is invalid")
    for volume in VOLUMES:
        member = bundle.getmember(f"{volume}.tar")
        payload = bundle.extractfile(member)
        if not isinstance(payload, io.BufferedReader):
            raise ValueError(f"Cold Milvus volume payload missing: {volume}")
        with payload:
            count = validate_volume_tar(payload)
            digest = hashlib.file_digest(payload, "sha256").hexdigest()
        if manifest["volumes"][volume] != {"members": count, "sha256": digest}:
            raise ValueError(f"Cold Milvus volume digest mismatch: {volume}")
    return manifest


def _volume_command(project: str, volume: str, image: str, journal: ResourceJournal, script: str, *, readonly: bool, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> None:
    _check_owned_volume(project, volume)
    owned_volume = f"{project}_{volume}"
    if subprocess.check_output(["docker", "ps", "--quiet", "--filter", f"volume={owned_volume}"], text=True, timeout=15).strip():
        raise ValueError("Stop all containers attached to a cold volume first")
    helper = f"{project}-archive-{uuid4().hex[:8]}"
    journal.event("restore-drill-reserve-volume-helper", project=project, container_name=helper, volume=owned_volume)
    mount = f"type=volume,source={owned_volume},target=/volume,volume-nocopy"
    if readonly:
        mount += ",readonly"
    command = ["docker", "run", "--name", helper, "--label", f"memory-cold-owner={project}", "--pull", "never", "--network", "none", "--read-only", "--user", "0:0", "--mount", mount]
    if stdin is not None:
        command.append("--interactive")
    try:
        subprocess.run([*command, "--entrypoint", "/bin/sh", image, "-ec", script], stdin=stdin or subprocess.DEVNULL, stdout=stdout, check=True, timeout=120)
    finally:
        names = subprocess.check_output(["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"label=memory-cold-owner={project}"], text=True, timeout=15).splitlines()
        if helper in names:
            subprocess.run(["docker", "rm", "--force", helper], check=True, timeout=20, stdout=subprocess.DEVNULL)
        remaining = subprocess.check_output(["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"label=memory-cold-owner={project}"], text=True, timeout=15).splitlines()
        if helper in remaining:
            raise RuntimeError("Cold archive helper container was not removed")
        journal.event("restore-drill-volume-helper-cleanup", project=project, container_name=helper, absent=True)


def archive_volumes(path: Path, project: str, profile: dict[str, object], image: str, journal: ResourceJournal) -> None:
    seals = {}
    with tarfile.open(path, "w") as bundle:
        os.chmod(path, 0o600)
        for volume in VOLUMES:
            scratch = path.with_name(f"{volume}.tar")
            journal.event("reserve-private-consumer-file", path=str(scratch), component=volume)
            try:
                with open(scratch, "x+b", opener=lambda filename, flags: os.open(filename, flags, 0o600)) as payload:
                    _volume_command(project, volume, image, journal, "tar --format=gnu --sort=name --numeric-owner -cf - -C /volume .", readonly=True, stdout=payload)
                    payload.seek(0)
                    count = validate_volume_tar(payload)
                    seals[volume] = {"members": count, "sha256": hashlib.file_digest(payload, "sha256").hexdigest()}
                    bundle.add(scratch, arcname=scratch.name, recursive=False)
            finally:
                scratch.unlink(missing_ok=True)
        data = json.dumps({"format": 1, "profile": profile, "volumes": seals}, sort_keys=True).encode()
        entry = tarfile.TarInfo(_MANIFEST)
        entry.size = len(data)
        bundle.addfile(entry, io.BytesIO(data))
    journal.event("milvus-cold-volume-archive", project=project, profile=profile, volumes=seals)


def restore_volumes(path: Path, project: str, profile: dict[str, object], image: str, journal: ResourceJournal) -> dict[str, object]:
    with tarfile.open(path, "r:") as bundle:
        manifest = validate_bundle(bundle)
        captured_profile = manifest["profile"]
        if cast(dict[str, object], captured_profile)["services"] != profile["services"]:
            raise ValueError("Cold Milvus archive and target service profiles differ")
        for volume in VOLUMES:
            scratch = path.with_name(f"{volume}-restore.tar")
            journal.event("reserve-private-consumer-file", path=str(scratch), component=volume)
            try:
                with open(scratch, "x+b", opener=lambda filename, flags: os.open(filename, flags, 0o600)) as stream, cast(BinaryIO, bundle.extractfile(f"{volume}.tar")) as payload:
                    shutil.copyfileobj(payload, stream, length=1024 * 1024)
                    stream.flush()
                    stream.seek(0)
                    _volume_command(project, volume, image, journal, 'test -z "$(ls -A /volume)" && tar --numeric-owner --same-owner --same-permissions -xf - -C /volume', readonly=False, stdin=stream)
                    stream.seek(0)
                    _volume_command(project, volume, image, journal, "tar --numeric-owner -df - -C /volume", readonly=True, stdin=stream)
            finally:
                scratch.unlink(missing_ok=True)
    journal.event("milvus-cold-volumes-verified", project=project, exact_members=True, content_and_metadata_match=True, volumes=VOLUMES)
    return cast(dict[str, object], captured_profile)
