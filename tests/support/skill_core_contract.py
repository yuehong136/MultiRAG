"""The same HTTP/CLI behavior contract runs against independent backend stores."""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests


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
    rebuilt = call("POST", core + "/reindex", json={"space_id": sid})
    assert rebuilt["indexed_count"] == 1 and rebuilt.get("failed_count", 0) == 0
    hits = call("POST", core + "/search", json={"space_id": sid, "query": "orange", "page": 1, "page_size": 20})
    assert hits["total"] == 1 and hits["skills"][0]["version"] == "2.0.0"
    assert hits["skills"][0]["folder_id"] == skill_folder
    call("DELETE", core + "/index", params={"space_id": sid, "skill_id": "orange"})
    assert call("POST", core + "/search", json={"space_id": sid, "query": "orange"})["total"] == 0
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
        result = subprocess.run([str(executable), "-h", address.netloc, "-t", token, *arguments], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    assert json.loads(cli("skill-core", "space", sid))["folder_id"] == folder
    assert "orange" in cli("ls", "skills/" + sid)
    assert "Orange instructions 1.0.0" in cli("cat", f"skills/{sid}/orange/1.0.0/SKILL.md")
    assert "orange attachment" in cli("cat", f"skills/{sid}/orange/1.0.0/references/中文.txt")
    local = directory / "local skill with spaces"
    local.mkdir()
    (local / "SKILL.md").write_text("---\nname: cli-core\ndescription: orange CLI core\n---\nOrange CLI instructions")
    assert json.loads(cli("install-skill", sid, str(local), "--version", "1.0.0"))["indexed"]
    assert json.loads(cli("uninstall-skill", sid, "cli-core"))["uninstalled"]
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
