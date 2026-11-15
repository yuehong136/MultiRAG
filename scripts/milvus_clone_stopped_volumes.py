"""Clone only stopped, owned Milvus rehearsal volumes into a new project.

This is a cold-copy rehearsal helper. It refuses live volumes, existing target
volumes, arbitrary project names, and volumes without the expected Compose
ownership labels. It never removes a source or target volume.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

_SUFFIXES = ("etcd_data", "minio_data", "milvus_data")
_HELPER_IMAGE = "milvusdb/milvus:v2.6.22"


def validate_projects(source: str, target: str) -> None:
    for name in (source, target):
        if not re.fullmatch(r"multirag-milvus-upgrade-[a-z0-9-]{1,50}", name):
            raise ValueError("Use only multirag-milvus-upgrade-* rehearsal projects")
    if source == target:
        raise ValueError("Source and target projects must differ")


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True).stdout.strip()


def _digest(volume: str) -> dict[str, Any]:
    output = _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "0:0",
        "--mount",
        f"type=volume,source={volume},target=/volume,readonly",
        "--entrypoint",
        "/bin/sh",
        _HELPER_IMAGE,
        "-c",
        "cd /volume && find . -type f -exec sha256sum {} +",
    )
    manifest = "\n".join(sorted(output.splitlines()))
    return {"files": len(manifest.splitlines()), "files_sha256": hashlib.sha256(manifest.encode()).hexdigest()}


def clone(source: str, target: str) -> list[dict[str, Any]]:
    validate_projects(source, target)
    source_volumes = [f"{source}_{suffix}" for suffix in _SUFFIXES]
    targets = [f"{target}_{suffix}" for suffix in _SUFFIXES]
    all_volumes = set(_docker("volume", "ls", "--format", "{{.Name}}").splitlines())
    if any(volume not in all_volumes for volume in source_volumes) or any(volume in all_volumes for volume in targets):
        raise ValueError("All source volumes must exist and every target volume must be new")
    for suffix, volume in zip(_SUFFIXES, source_volumes, strict=True):
        metadata = json.loads(_docker("volume", "inspect", volume))[0]
        labels = metadata.get("Labels") or {}
        if labels.get("com.docker.compose.project") != source or labels.get("com.docker.compose.volume") != suffix:
            raise ValueError("Source volume is not owned by the expected rehearsal project")
        if _docker("ps", "--quiet", "--filter", f"volume={volume}"):
            raise ValueError("Stop every container using a source volume before cold copying")
    receipts: list[dict[str, Any]] = []
    for suffix, source_volume, target_volume in zip(_SUFFIXES, source_volumes, targets, strict=True):
        before = _digest(source_volume)
        _docker("volume", "create", "--label", f"com.docker.compose.project={target}", "--label", f"com.docker.compose.volume={suffix}", target_volume)
        _docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "--mount",
            f"type=volume,source={source_volume},target=/source,readonly",
            "--mount",
            f"type=volume,source={target_volume},target=/target",
            "--entrypoint",
            "/bin/sh",
            _HELPER_IMAGE,
            "-c",
            'test -z "$(ls -A /target)" && cp -a /source/. /target/',
        )
        after = _digest(target_volume)
        if before != after or before != _digest(source_volume):
            raise AssertionError("Cold-copy file manifests do not match")
        receipts.append({"source_volume": source_volume, "target_volume": target_volume, **after})
    return receipts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-project", required=True)
    parser.add_argument("--target-project", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    receipts = clone(args.source_project, args.target_project)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipts, indent=2) + "\n")
    print(json.dumps(receipts))


if __name__ == "__main__":
    main()
