"""Real HTTP MCP Skills consumers over isolated PostgreSQL and object storage."""

import asyncio
import importlib.util
import socket
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.utilities.skills import download_skill, get_skill_manifest, list_skills

from mcp.shared.exceptions import MCPError
from tests.support.skills_http import bootstrapped_engine as bootstrapped_engine
from tests.support.skills_http import image_http_api as image_http_api
from tests.support.skills_http import image_http_database as image_http_database
from tests.support.skills_http import image_resources as image_resources
from tests.support.skills_http import install, skill_complete, skill_request
from tests.support.skills_http import skill_http as skill_http


def test_real_skill_distribution_and_tenant_revocation(skill_http: dict[str, Any], tmp_path: Path) -> None:
    env = skill_http
    space = skill_request(env, "POST", "/spaces", body={"name": "MCP distribution"})
    completed = skill_complete(env, install(env, space["id"], "1.0.0"))
    spec = importlib.util.spec_from_file_location("multirag_skill_live_mcp", Path(__file__).resolve().parents[2] / "mcp/server/server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.MODE = module.LaunchMode.HOST
    module.BASE_URL = env["base"]
    module.SKILLS_RESOURCES_ENABLED = True
    module.TRANSPORT_SSE_ENABLED = False
    module.RATE_LIMIT_RPS = 10000.0
    module.create_mcp_server()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    service = uvicorn.Server(uvicorn.Config(module.create_starlette_app(), log_level="error", access_log=False))

    async def consume() -> None:
        task = asyncio.create_task(service.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(30):
                while not service.started:
                    assert not task.done()
                    await asyncio.sleep(0.01)
            address = f"http://127.0.0.1:{port}/mcp"
            async with Client(address, auth=env["skill_api_token"]) as client:
                skills = await list_skills(client)
                assert len(skills) == 1
                skill = skills[0]
                assert completed["result"]["version_id"] in skill.uri
                manifest = await get_skill_manifest(client, skill.name)
                assert len(manifest.files) == 3
                downloaded = await download_skill(client, skill.name, tmp_path)
                assert (downloaded / "nested/中文.txt").read_bytes() == b"orange nested bytes"
                assert (downloaded / "image.bin").read_bytes() == b"\xff\x00"
                async with Client(address, auth=env["tokens"]["api"]) as other:
                    assert await list_skills(other) == []
                    with pytest.raises(MCPError):
                        await other.read_resource(skill.uri)
                await asyncio.to_thread(skill_request, env, "DELETE", "/spaces/" + space["id"], key="mcp-delete", expected=202)
                assert await list_skills(client) == []
                with pytest.raises(MCPError):
                    await client.read_resource(skill.uri)
        finally:
            service.should_exit = True
            async with asyncio.timeout(15):
                await task
            listener.close()
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", port)) != 0

    asyncio.run(consume())
    skill_complete(env, skill_request(env, "DELETE", "/spaces/" + space["id"], key="mcp-delete", expected=202))
