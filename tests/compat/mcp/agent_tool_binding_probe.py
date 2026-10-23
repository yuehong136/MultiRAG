"""Real Agent registration -> two loopback MCP servers -> canonical dispatch.

Run with the project interpreter and PYTHONPATH=.; --report-dir retains raw
server logs, ownership, call readback and cleanup even after an assertion fails.
No model, database, signing key or third-party MCP credential is needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import socket
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from common import resources
from common.constants import MCPServerType
from common.mcp_tool_call_conn import MCPToolBinding, MCPToolCallSession
from scripts.check_mcp_compat import MODERN_SERVER, ManagedServer, _read_json


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


async def run_probe(report_dir: Path) -> dict[str, Any]:
    # Same closed resource boundary as unit conftest; accidental access fails.
    resources._state.update(secret_key="loopback-agent-probe", doc_store=object(), msg_store=object(), storage=object(), retriever=object(), kg_retriever=object())
    from tests.support.agent_tool_binding import CANONICAL_TOOLS, assert_registration, delegation_service, registered_agent

    uv = shutil.which("uv")
    assert uv is not None
    managed = {key: ManagedServer(f"agent-binding-{key}", [uv, "run", "--locked", "--no-project", "--script", str(MODERN_SERVER), "--serve", "--binding-server-id", key]) for key in CANONICAL_TOOLS}
    servers: dict[str, SimpleNamespace] = {}
    metadata: dict[str, dict[str, Any]] = {}
    sessions: list[MCPToolCallSession] = []
    result: dict[str, Any] = {"status": "RUNNING", "ownership": []}
    report_dir.mkdir(parents=True, exist_ok=True)
    baseline_threads = set(threading.enumerate())
    try:
        for key, server in managed.items():
            ready = server.start()
            assert server.process is not None
            result["ownership"].append({"server_id": key, "pid": server.process.pid, "url": ready["protected_url"]})
            (report_dir / "ownership.json").write_text(json.dumps(result["ownership"], indent=2))
            servers[key] = SimpleNamespace(id=key, tenant_id="tenant-probe", url=ready["protected_url"], server_type=MCPServerType.STREAMABLE_HTTP, headers={}, variables={})
            discovery = MCPToolCallSession(SimpleNamespace(id=f"discover-{key}", url=ready["url"], server_type=MCPServerType.STREAMABLE_HTTP, headers={}))
            sessions.append(discovery)
            assert discovery.wait_ready(10)
            try:
                tools = {tool.name: tool for tool in discovery.get_tools(timeout=5)}
                metadata[key] = {name: tools[name].model_dump(mode="json", by_alias=True, exclude_none=True) for name in CANONICAL_TOOLS[key]}
            finally:
                discovery.close_sync(timeout=3)

        before = deepcopy(metadata)
        service, issuer = delegation_service(servers)

        def readback() -> dict[str, Any]:
            return {key: _read_json(server.ready["url"].removesuffix("/mcp") + "/__compat/bindings") for key, server in managed.items()}

        expected: dict[str, list[dict[str, Any]]] = {key: [] for key in servers}
        aliases_by_user = {}
        for user in ("alice", "bob"):
            initial = readback()
            issued_before = len(issuer.requests)
            with registered_agent(servers, metadata, service, user=user) as registered:
                agent = registered.agent
                sessions.extend(agent._mcp_sessions)
                aliases = assert_registration(registered, metadata)
                expected_names = {("a", "compat_echo_2"), ("a", "compat_echo"), ("a", "compat_echo_0"), ("b", "compat_echo")} if user == "alice" else {("b", "compat_echo")}
                assert set(aliases) == expected_names
                assert metadata == before
                assert len(issuer.requests) == issued_before
                assert readback() == initial, "registration/visibility must not issue tokens or make delegated requests"
                aliases_by_user[user] = [{"server": key, "canonical": canonical, "alias": alias} for (key, canonical), alias in aliases.items()]
                for (key, canonical), alias in aliases.items():
                    arguments = {"value": f"{user}:{key}:{canonical}"}
                    text = await agent.toolcall_session.tool_call_async(alias, arguments)
                    binding = agent.tools[alias]
                    assert isinstance(binding, MCPToolBinding)
                    assert binding.session.call_context.principal.platform_user_id == user
                    meta = binding.session.get_last_tool_call_meta()
                    assert meta is not None and meta["is_error"] is False
                    structured = meta["structured_content"]
                    actual = {field: structured[field] for field in ("server_id", "user", "canonical_name", "arguments")}
                    call = {"server_id": key, "user": user, "canonical_name": canonical, "arguments": arguments}
                    assert actual == call
                    expected[key].append(call)
                    assert registered.callback.call_args.args == ("binding-probe", alias, arguments, text)
                    assert registered.callback.call_args.kwargs["elapsed_time"] >= 0
                    assert issuer.requests[-1].principal.platform_user_id == user
                    assert issuer.requests[-1].resource_name == f"resource-{key}"
                    assert issuer.requests[-1].requested_scopes == frozenset({"probe:read"})

                denied_before, issued_before = readback(), len(issuer.requests)
                callbacks_before = registered.callback.call_count
                for forbidden in ("compat_fail", "unknown_model_alias"):
                    try:
                        await agent.toolcall_session.tool_call_async(forbidden, {})
                    except AssertionError:
                        pass
                    else:
                        raise AssertionError("unexposed tool was callable")
                # Even bypassing the alias router cannot authorize a denied
                # canonical tool or use an exported alias as policy authority.
                first_binding = next(binding for binding in agent.tools.values() if isinstance(binding, MCPToolBinding))
                for forbidden in ("compat_fail", "unknown_model_alias"):
                    text = first_binding.session.tool_call(forbidden, {}, timeout=1)
                    assert "MCP delegation rejected" in text
                if user == "alice":
                    duplicate_alias = aliases[("a", "compat_echo")]
                    assert duplicate_alias not in CANONICAL_TOOLS["a"]
                    text = first_binding.session.tool_call(duplicate_alias, {}, timeout=1)
                    assert "MCP delegation rejected" in text
                else:
                    # The same canonical name is granted on B, denied on A.
                    text = agent._mcp_sessions[0].tool_call("compat_echo", {"value": "wrong-resource"}, timeout=1)
                    assert "MCP delegation rejected" in text
                assert len(issuer.requests) == issued_before
                assert registered.callback.call_count == callbacks_before
                assert readback() == denied_before, "denials must make zero remote HTTP requests, including discovery"
                assert await agent.toolcall_session.tool_call_async("compat_echo_0", {"value": user}) == "native-result"
                assert readback() == denied_before
            assert metadata == before

        final = readback()
        assert {key: state["calls"] for key, state in final.items()} == expected
        result.update(status="PASS", aliases=aliases_by_user, remote_readback=final, issued_requests=len(issuer.requests), metadata_unchanged=metadata == before)
        return result
    finally:
        for session in sessions:
            session.close_sync(timeout=3)
        for key, server in managed.items():
            server.stop()
            (report_dir / f"server-{key}.log").write_text("\n".join(server.output))
        cleanup = {
            "sessions_closed": all(session._close and session._loop_thread_future.done() and not session._inflight_tasks and session not in MCPToolCallSession._ALL_INSTANCES for session in sessions),
            "processes_exited": all(server.process is None or server.process.poll() is not None for server in managed.values()),
            "ports_closed": all(not _listening(urlsplit(item["url"]).port) for item in result["ownership"]),
            "owner_threads_stopped": not any(thread not in baseline_threads and thread.name.startswith("multirag-mcp") for thread in threading.enumerate()),
        }
        result["cleanup"] = cleanup
        (report_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        assert all(cleanup.values()), cleanup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    print(json.dumps(asyncio.run(run_probe(args.report_dir)), sort_keys=True))


if __name__ == "__main__":
    main()
