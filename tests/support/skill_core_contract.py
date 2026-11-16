"""The same HTTP/CLI behavior contract runs against independent backend stores."""

import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from tests.support.resource_journal import ResourceJournal


def exercise_core_protocol_process() -> None:
    """Select the public core alias before a fresh application imports routes."""
    journal = ResourceJournal("canonical-core-cli-protocol")
    node = "tests/integration/test_skill_core.py::test_core_shared_http_cli_contract"
    command = [sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", node, "-q", "--tb=short"]
    environment = {
        **os.environ,
        "SKILLS_API_PROTOCOL": "ragflow-skills-v1",
        "MULTIRAG_RUN_ID": journal.run_id,
        "MULTIRAG_RUN_JOURNAL": str(journal.path),
    }
    paths = [
        "tests/integration/test_skill_core.py",
        "tests/support/skill_core_contract.py",
        "tests/support/skills_http.py",
        "api/skills/protocol.py",
        "api/apps/restful_apis/skill_core_api.py",
        "api/skills/core_service.py",
        "cmd/multirag_cli.go",
        "internal/cli/cli.go",
        "internal/cli/parser.go",
        "internal/cli/lexer.go",
        "internal/cli/filesystem/skill.go",
        "internal/cli/filesystem/skill_install.go",
        "internal/cli/filesystem/skill_uninstall.go",
        "internal/cli/filesystem/file.go",
        "go.mod",
        "go.sum",
        "pyproject.toml",
        "uv.lock",
    ]
    journal.event(
        "reserve-core-cli-protocol-process",
        command=command,
        environment_sha256=hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest(),
        protocol=environment["SKILLS_API_PROTOCOL"],
        sources={name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in paths},
        process_group=os.getpgrp(),
    )
    # The child journals its real PID before exec and remains in the parent's
    # canonical runner process group, including its compiler/CLI descendants.
    result = subprocess.run([sys.executable, "-B", "scripts/run_registered.py", "--child", str(journal.path), *command], env=environment, capture_output=True, text=True, timeout=360)
    journal.event("core-cli-protocol-process-exit", raw_exit=result.returncode)
    assert result.returncode == 0, result.stdout + result.stderr


def exercise_skill_core(env: dict[str, Any], directory: Path) -> str:
    """No private tables, worker IDs, or physical index format enter this contract."""
    origin, token = env["base"], env["skill_api_token"]
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + token

    def call(method: str, path: str, expected: int = 200, **kwargs: Any) -> Any:
        response = session.request(method, origin + "/api/v1" + path, timeout=90, **kwargs)
        assert response.status_code == expected, (path, response.status_code, response.text)
        if "application/json" not in response.headers.get("Content-Type", ""):
            return response.content
        payload = response.json()
        assert payload["code"] == 0, (path, payload)
        return payload["data"]

    core = "/skill-core"
    space = call("POST", core + "/spaces", json={"name": "Independent core contract"})
    sid, folder = space["id"], space["folder_id"]
    assert space["status"] == "active" and "operation_id" not in space
    assert call("GET", core + "/space/by-folder", params={"folder_id": folder})["id"] == sid
    assert any(row["id"] == sid for row in call("GET", core + "/spaces")["spaces"])
    call("PUT", core + "/spaces/" + sid, json={"name": "Core acceptance renamed"})
    assert call("GET", core + "/spaces/" + sid)["name"] == "Core acceptance renamed"
    config = {"space_id": sid, "embd_id": env["skill_models"][0], "vector_similarity_weight": 0.5, "similarity_threshold": 0.0, "top_k": 20}
    call("POST", core + "/config", json=config)
    assert call("GET", core + "/config", params={"space_id": sid})["embd_id"] == env["skill_models"][0]

    def mkdir(parent: str, name: str) -> str:
        return str(call("POST", "/files", json={"parent_id": parent, "name": name, "type": "folder"})["id"])

    def children(parent: str) -> list[dict[str, Any]]:
        data = call("GET", "/files", params={"parent_id": parent, "page": 1, "page_size": 100})
        assert len(data["files"]) == data["total"]
        return data["files"]

    skill_folder = mkdir(folder, "orange")
    versions: dict[str, str] = {}
    # Upload an older version last; reindex still chooses the highest version.
    for version in ("2.0.0", "1.0.0"):
        versions[version] = mkdir(skill_folder, version)
        source = f"---\nname: orange\ndescription: orange {version}\n---\nOrange instructions {version}"
        call(
            "POST",
            "/files",
            data={"parent_id": versions[version]},
            files=[("file", ("SKILL.md", source.encode(), "text/markdown")), ("file", ("references/中文.txt", b"orange attachment", "text/plain"))],
        )
    assert len(children(versions["2.0.0"])) == 2
    unindexed = call("POST", core + "/search", json={"space_id": sid, "query": ""})
    assert unindexed["total"] == 0  # Files, not index search, lists unindexed directories.
    rebuilt = call("POST", core + "/reindex", json={"space_id": sid})
    assert rebuilt["indexed_count"] == 1 and rebuilt.get("failed_count", 0) == 0
    hits = call("POST", core + "/search", json={"space_id": sid, "query": "orange", "page": 1, "page_size": 20})
    assert hits["total"] == 1 and hits["skills"][0]["version"] == "2.0.0"
    assert hits["skills"][0]["folder_id"] == skill_folder
    call("DELETE", core + "/index", params={"space_id": sid, "skill_id": "orange"})
    assert call("POST", core + "/search", json={"space_id": sid, "query": "orange"})["total"] == 0
    assert call("POST", core + "/search", json={"space_id": sid, "query": ""})["total"] == 0
    assert len(children(skill_folder)) == 2
    call("POST", core + "/reindex", json={"space_id": sid})
    call("DELETE", "/files", json={"file_ids": [versions["2.0.0"]]})
    assert all(row["name"] != "2.0.0" for row in children(skill_folder))
    # A Files deletion must not leave an indexed reference to the deleted version.
    remaining = call("POST", core + "/search", json={"space_id": sid, "query": "orange"})
    assert all(row["version"] != "2.0.0" for row in remaining["skills"])
    call("POST", core + "/reindex", json={"space_id": sid})
    assert call("POST", core + "/search", json={"space_id": sid, "query": "orange"})["skills"][0]["version"] == "1.0.0"

    executable = directory / "multirag-core-cli"
    subprocess.run([os.environ.get("MULTIRAG_TEST_GO", "go"), "build", "-o", str(executable), "./cmd/multirag_cli.go"], check=True, capture_output=True, text=True, timeout=180)
    address = urlsplit(origin)

    def cli(*arguments: str) -> str:
        # The canonical CLI parses one command string; quote path/name arguments
        # inside that command, including the local directory with spaces.
        result = subprocess.run([str(executable), "-h", address.netloc, "-t", token, shlex.join(arguments)], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    space_name = call("GET", core + "/spaces/" + sid)["name"]
    assert space_name in cli("ls", "skills")
    assert "orange" in cli("ls", "skills/" + space_name)
    assert "Orange instructions 1.0.0" in cli("cat", f"skills/{space_name}/orange/1.0.0/SKILL.md")
    assert "orange attachment" in cli("cat", f"skills/{space_name}/orange/1.0.0/references/中文.txt")
    local = directory / "local skill with spaces"
    local.mkdir()
    (local / "SKILL.md").write_text("---\nname: cli-core\nversion: 1.0.0\ndescription: orange CLI core\n---\nOrange CLI instructions")
    (local / "references").mkdir()
    (local / "references/中文.txt").write_text("CLI attachment bytes")
    installed = cli("install-skill", space_name, str(local))
    assert "Successfully installed skill 'cli-core'" in installed
    assert "Warning: failed to update index" not in installed, installed
    indexed = call("POST", core + "/search", json={"space_id": sid, "query": ""})
    assert indexed["total"] == 2 and {row["name"] for row in indexed["skills"]} == {"orange", "cli-core"}, indexed
    assert "CLI attachment bytes" in cli("cat", f"skills/{space_name}/cli-core/1.0.0/references/中文.txt")
    installed_folder = next(row["id"] for row in children(folder) if row["name"] == "cli-core")
    installed_version = next(row["id"] for row in children(installed_folder) if row["name"] == "1.0.0")
    references = next(row["id"] for row in children(installed_version) if row["name"] == "references")
    attachment = children(references)
    assert len(attachment) == 1 and attachment[0]["name"] == "中文.txt"
    assert call("GET", "/files/" + attachment[0]["id"]) == b"CLI attachment bytes"
    uninstalled = cli("uninstall-skill", space_name, "cli-core")
    assert "Successfully uninstalled skill 'cli-core'" in uninstalled
    remaining_index = call("POST", core + "/search", json={"space_id": sid, "query": ""})
    assert remaining_index["total"] == 1 and [row["name"] for row in remaining_index["skills"]] == ["orange"], remaining_index
    assert all(row["name"] != "cli-core" for row in children(folder))
    call("DELETE", "/files", json={"ids": [skill_folder]})
    assert call("POST", core + "/search", json={"space_id": sid, "query": "orange"})["total"] == 0
    accepted = call("DELETE", core + "/spaces/" + sid, expected=202)
    assert accepted == {"deleting": True, "space_id": sid}
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = session.get(origin + "/api/v1" + core + "/spaces/" + sid, timeout=30)
        if response.status_code == 404:
            break
        assert response.status_code == 200 and response.json()["data"]["status"] == "deleting", response.text
        time.sleep(0.1)
    else:
        raise AssertionError("space deletion did not complete")
    session.close()
    return sid
