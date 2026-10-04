"""One real CLI consumer scenario, reusable against either backend."""

import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def exercise_skills_cli(env: dict[str, Any], directory: Path) -> str:
    executable = directory / "multirag_cli"
    go = os.environ.get("MULTIRAG_TEST_GO", "go")
    subprocess.run([go, "build", "-o", str(executable), "./cmd/multirag_cli.go"], check=True, capture_output=True, text=True, timeout=120)
    address = urlsplit(env["base"])

    def run(*arguments: str) -> Any:
        result = subprocess.run([str(executable), "-h", address.netloc, "-t", env["skill_api_token"], "skills", *arguments], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout) if result.stdout.strip() else None

    package = directory / "package with spaces"
    (package / "nested").mkdir(parents=True)
    files = {"SKILL.md": b"---\nname: cli-orange\ndescription: orange CLI package\ntags: [fruit]\n---\norange guide", "nested/中文.txt": b"orange nested bytes", "asset.bin": b"\xff\x01"}
    for name, data in files.items():
        (package / name).write_bytes(data)
    space = run("create-space", "CLI space with spaces")
    sid = space["id"]
    config = run("config", sid)
    config_path = directory / "config.json"
    config_path.write_text(json.dumps({"revision": config["revision"], "embedding_model_id": env["skill_models"][0], "rerank_model_id": env["skill_models"][2]}))
    run("set-config", sid, str(config_path))
    accepted = run("install", sid, str(package), "cli-orange", "1.0.0", "--activate", "--key", "cli-install")
    finished = run("wait", accepted["operation_id"])
    assert finished["state"] == "succeeded" and finished["result"]["skipped_binary_count"] == 1
    assert run("install", sid, str(package), "cli-orange", "1.0.0", "--activate", "--key", "cli-install")["operation_id"] == accepted["operation_id"]
    assert run("list", sid)["total"] == 1
    assert run("search", sid, "orange", "hybrid")["skills"][0]["score"] == 0.95
    archive_path = directory / "download.zip"
    run("download", sid, finished["result"]["version_id"], str(archive_path))
    with zipfile.ZipFile(archive_path) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == files
    accepted = run("delete-space", sid, "--key", "cli-delete")
    assert run("wait", accepted["operation_id"])["state"] == "succeeded"
    return sid
