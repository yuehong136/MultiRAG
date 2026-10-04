"""FastMCP's actual Skills client consumes tenant-bound, fixed-version assets."""

import hashlib
import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.utilities.skills import download_skill, get_skill_manifest, list_skills

from mcp.shared.exceptions import MCPError

_PATH = Path(__file__).resolve().parents[2] / "mcp/server/server.py"
_SPEC = importlib.util.spec_from_file_location("multirag_mcp_skill_test", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)
SPACE, SKILL, VERSION = "a" * 32, "b" * 32, "c" * 32


@pytest.fixture
def assets(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"token": "tenant-one", "deleted": False, "corrupt": False, "calls": []}
    files = {"SKILL.md": b"---\nname: demo\ndescription: Demo\n---\nRead these instructions.", "references/\u4e2d\u6587.txt": b"Attachment", "asset.bin": b"\x00\xff"}
    state["files"] = files

    def respond(request: httpx.Request) -> httpx.Response:
        state["calls"].append(request)
        if request.headers.get("authorization") != "Bearer tenant-one" or state["deleted"]:
            return httpx.Response(404, json={"code": 404, "data": {}})
        path = request.url.path
        data: dict[str, Any]
        if path.endswith("/spaces"):
            data = {"spaces": [{"id": SPACE}], "total": 1}
        elif path.endswith("/skills"):
            data = {"skills": [{"id": SKILL, "state": "active", "description": "Demo", "active_version_id": VERSION}], "total": 1}
        elif path.endswith("/skills/" + SKILL):
            data = {"skill": {"state": "active"}, "versions": [{"id": VERSION, "state": "installed"}]}
        elif path.endswith("/files"):
            data = {"files": [{"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()} for name, content in files.items()]}
        elif path.endswith("/file"):
            content = b"tampered" if state["corrupt"] else files[request.url.params["path"]]
            return httpx.Response(200, content=content)
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 0, "data": data})

    connector = server.MultiRAGConnector("http://skill-test")
    connector._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(server, "_get_connector", lambda: connector)
    monkeypatch.setattr(server, "_resolve_api_key", lambda: state["token"])
    monkeypatch.setattr(server, "SKILLS_RESOURCES_ENABLED", True)
    monkeypatch.setattr(server, "RATE_LIMIT_RPS", 10000.0)
    state["connector"] = connector
    return state


async def test_official_skill_client_discovers_manifests_and_downloads(assets: dict[str, Any], tmp_path: Path) -> None:
    async with Client(server.create_mcp_server()) as client:
        discovered = await list_skills(client)
        assert len(discovered) == 1 and discovered[0].description == "Demo"
        # Discovery transfers metadata only, not every body/attachment.
        assert all(not request.url.path.endswith(("/file", "/files")) for request in assets["calls"])
        manifest = await get_skill_manifest(client, discovered[0].name)
        assert {f.path for f in manifest.files} == set(assets["files"])
        target = await download_skill(client, discovered[0].name, tmp_path)
        assert {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()} == assets["files"]
        assert all("/skill-assets/" in request.url.path for request in assets["calls"])
    await assets["connector"].close()


async def test_reauthorizes_fixed_uri_and_rejects_changed_bytes(assets: dict[str, Any]) -> None:
    async with Client(server.create_mcp_server()) as client:
        discovered = await list_skills(client)
        uri = discovered[0].uri
        assets["token"] = "tenant-two"
        with pytest.raises(MCPError, match=r"Error|unavailable"):
            await client.read_resource(uri)
        assets["token"] = "tenant-one"
        assets["corrupt"] = True
        with pytest.raises(MCPError, match=r"Error|manifest"):
            await client.read_resource(uri)
        assets["corrupt"] = False
        assets["deleted"] = True
        with pytest.raises(MCPError, match=r"Error|unavailable"):
            await client.read_resource(uri)
    await assets["connector"].close()


async def test_official_download_preserves_literal_percent_paths(assets: dict[str, Any], tmp_path: Path) -> None:
    assets["files"].update({"a%2Fb.txt": b"literal percent", "a/b.txt": b"nested", "has space.txt": b"space"})
    async with Client(server.create_mcp_server()) as client:
        skill = (await list_skills(client))[0]
        target = await download_skill(client, skill.name, tmp_path)
        assert {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()} == assets["files"]
        encoded = await client.read_resource(f"skill://{SPACE}-{SKILL}-{VERSION}/a%252Fb.txt")
        assert encoded[0].blob == "bGl0ZXJhbCBwZXJjZW50"
    await assets["connector"].close()


async def test_reserved_manifest_file_refuses_download_without_replacing_bytes(assets: dict[str, Any], tmp_path: Path) -> None:
    assets["files"]["_manifest"] = b"real attachment"
    async with Client(server.create_mcp_server()) as client:
        skill = (await list_skills(client))[0]
        with pytest.raises(MCPError):
            await download_skill(client, skill.name, tmp_path)
        assert not any(p.is_file() for p in tmp_path.rglob("*"))
        assert all(not request.url.path.endswith("/file") for request in assets["calls"])
    assert assets["files"]["_manifest"] == b"real attachment"
    await assets["connector"].close()


async def test_skill_resources_opt_in_and_uri_cannot_escape(assets: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    provider = server.SkillAssetProvider()
    assert await provider._get_resource(f"skill://{SPACE}-{SKILL}-{VERSION}/%2e%2e/secret") is None
    assert await provider._get_resource(f"skill://{SPACE}-{SKILL}-{VERSION}/a%5cb") is None
    monkeypatch.setattr(server, "SKILLS_RESOURCES_ENABLED", False)
    assert await provider._list_resources() == []
    assert await provider._get_resource(f"skill://{SPACE}-{SKILL}-{VERSION}/SKILL.md") is None
    assert assets["calls"] == []
    await assets["connector"].close()
