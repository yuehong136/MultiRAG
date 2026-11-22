"""Regression checks for the physical Milvus rehearsal archive boundaries."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

from tests.manual.memory_milvus_cold_volumes import VOLUMES, validate_bundle, validate_volume_tar


def _entry(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    archive.addfile(member, io.BytesIO(data))


def _volume(name: str, kind: bytes) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        member = tarfile.TarInfo(name)
        member.type = kind
        if kind == tarfile.SYMTYPE:
            member.linkname = "/outside"
        archive.addfile(member)
    return payload.getvalue()


@pytest.mark.parametrize(
    ("name", "kind", "reason"),
    [
        ("../outside", tarfile.REGTYPE, "Unsupported member"),
        ("/outside", tarfile.REGTYPE, "Unsupported member"),
        ("linked", tarfile.SYMTYPE, "Unsupported member"),
        ("missing-parent/payload", tarfile.REGTYPE, "Incomplete directory tree"),
    ],
)
def test_cold_volume_rejects_unsafe_or_incomplete_paths(name: str, kind: bytes, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        validate_volume_tar(io.BytesIO(_volume(name, kind)))


@pytest.mark.parametrize(
    ("variant", "reason"),
    [("complete", None), ("missing-declared-volume", "volume set incomplete"), ("wrong-inner-digest", "volume digest mismatch")],
)
def test_cold_bundle_requires_every_volume_and_valid_inner_seals(variant: str, reason: str | None) -> None:
    payload = io.BytesIO()
    volume = _volume("metadata", tarfile.REGTYPE)
    seals = {name: {"members": 2, "sha256": hashlib.sha256(volume).hexdigest()} for name in VOLUMES}
    if variant == "wrong-inner-digest":
        seals["etcd_data"]["sha256"] = "0" * 64
    manifest = {"format": 1, "profile": {"services": {}}, "volumes": seals}
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name in VOLUMES:
            if variant == "missing-declared-volume" and name == "minio_image_data":
                continue
            _entry(archive, f"{name}.tar", volume)
        _entry(archive, "milvus-cold.json", json.dumps(manifest).encode())
    payload.seek(0)
    with tarfile.open(fileobj=payload, mode="r") as archive:
        if reason is None:
            assert validate_bundle(archive) == manifest
        else:
            with pytest.raises(ValueError, match=reason):
                validate_bundle(archive)
