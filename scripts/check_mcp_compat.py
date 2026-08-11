#!/usr/bin/env python3
"""Run the EIM-F2 real-process MCP protocol compatibility matrix."""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LEGACY_SERVER = ROOT / "tests" / "compat" / "mcp" / "legacy_server.py"
MODERN_SERVER = ROOT / "tests" / "compat" / "mcp" / "modern_server.py"
CURRENT_PROBE = ROOT / "tests" / "compat" / "mcp" / "current_client_probe.py"
MULTIRAG_SERVER = ROOT / "tests" / "compat" / "mcp" / "multirag_server.py"


@dataclass
class ManagedServer:
    name: str
    command: list[str]
    process: subprocess.Popen[str] | None = None
    lines: queue.Queue[str] = field(default_factory=queue.Queue)
    output: list[str] = field(default_factory=list)
    ready: dict[str, Any] | None = None

    def start(self, timeout: float = 120) -> dict[str, Any]:
        self.process = subprocess.Popen(
            self.command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert self.process.stdout is not None

        def drain() -> None:
            assert self.process is not None and self.process.stdout is not None
            for line in self.process.stdout:
                clean = line.rstrip()
                self.output.append(clean)
                self.lines.put(clean)

        threading.Thread(target=drain, name=f"{self.name}-output", daemon=True).start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert self.process is not None
            if self.process.poll() is not None:
                raise RuntimeError(f"{self.name} exited before ready: {self._tail()}")
            try:
                line = self.lines.get(timeout=min(0.2, deadline - time.monotonic()))
            except queue.Empty:
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if candidate.get("event") == "ready":
                self.ready = candidate
                return candidate
        raise TimeoutError(f"{self.name} did not become ready: {self._tail()}")

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def _tail(self) -> str:
        return " | ".join(self.output[-8:])


def _last_json_line(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(completed.stdout.splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"probe emitted no JSON (exit={completed.returncode}): stdout={completed.stdout[-1000:]!r} stderr={completed.stderr[-1000:]!r}")


def _run(command: list[str], timeout: float = 30) -> dict[str, Any]:
    python_path = os.environ.get("PYTHONPATH")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) if not python_path else f"{ROOT}{os.pathsep}{python_path}"
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        env=env,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"probe failed ({completed.returncode}): {' '.join(command)}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
    return _last_json_line(completed)


def _current_probe(
    *,
    url: str,
    transport: str,
    scenario: str,
    auth: str = "none",
) -> dict[str, Any]:
    return _run(
        [
            sys.executable,
            str(CURRENT_PROBE),
            "--url",
            url,
            "--transport",
            transport,
            "--scenario",
            scenario,
            "--auth",
            auth,
        ],
        timeout=15,
    )


def _modern_probe(
    uv: str,
    *,
    url: str,
    mode: str,
    cancel: bool = False,
    list_only: bool = False,
    token: str | None = None,
    tool: str | None = None,
) -> dict[str, Any]:
    command = [
        uv,
        "run",
        "--locked",
        "--no-project",
        "--script",
        str(MODERN_SERVER),
        "--probe",
        url,
        "--mode",
        mode,
    ]
    if cancel:
        command.append("--cancel")
    if list_only:
        command.append("--list-only")
    if token is not None:
        command.extend(("--token", token))
    if tool is not None:
        command.extend(("--tool", tool))
    return _run(command, timeout=30)


def _read_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read())


def _raw_status(url: str, token: str | None) -> int:
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def _row(
    row_id: str,
    client: str,
    server: str,
    protocol: str,
    transport: str,
    passed: bool,
    outcome: str,
    evidence: Any,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "client": client,
        "server": server,
        "protocol": protocol,
        "transport": transport,
        "status": "PASS" if passed else "FAIL",
        "outcome": outcome,
        "evidence": evidence,
    }


def run_matrix() -> list[dict[str, Any]]:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required for the isolated MCP SDK 2 fixture")
    modern_command = [
        uv,
        "run",
        "--locked",
        "--no-project",
        "--script",
        str(MODERN_SERVER),
        "--serve",
    ]
    legacy_command = [
        uv,
        "run",
        "--locked",
        "--no-project",
        "--script",
        str(LEGACY_SERVER),
    ]
    servers = [
        ManagedServer("legacy-http", [*legacy_command, "--transport", "http"]),
        ManagedServer("legacy-sse", [*legacy_command, "--transport", "sse"]),
        ManagedServer("multirag-current", [sys.executable, str(MULTIRAG_SERVER)]),
        ManagedServer("modern-http", modern_command),
    ]
    rows: list[dict[str, Any]] = []
    try:
        legacy_http, legacy_sse, multirag_current, modern = (server.start() for server in servers)

        legacy_sse_result = _current_probe(url=legacy_sse["url"], transport="sse", scenario="echo")
        rows.append(
            _row(
                "legacy-sse-success",
                "MultiRAG MCP SDK 2 legacy mode",
                "FastMCP 3",
                str(legacy_sse_result.get("protocol_version")),
                "SSE",
                legacy_sse_result.get("structured_content", {}).get("server_era") == "legacy",
                "structured echo",
                legacy_sse_result.get("structured_content"),
            )
        )

        inbound_current = _modern_probe(
            uv,
            url=multirag_current["url"],
            mode="auto",
            token="eim-f2-multirag",
            tool="list_datasets",
        )
        inbound_tools = inbound_current.get("tools", [])
        inbound_headers = _read_json(multirag_current["headers_url"])
        inbound_ok = (
            inbound_current.get("protocol_version") == "2026-07-28"
            and inbound_tools == ["list_datasets", "multirag_retrieval"]
            and inbound_current.get("tool_result") == {"result": []}
            and inbound_headers.get("mcp-protocol-version") == "2026-07-28"
            and inbound_headers.get("mcp-method") == "tools/call"
            and inbound_headers.get("mcp-name") == "list_datasets"
            and "mcp-session-id" not in inbound_headers
        )
        rows.append(
            _row(
                "modern-client-to-multirag",
                "MCP Python SDK 2 auto",
                "MultiRAG FastMCP 4",
                str(inbound_current.get("protocol_version")),
                "stateless HTTP",
                inbound_ok,
                "real inbound server uses modern server/discover",
                {
                    "protocol_version": inbound_current.get("protocol_version"),
                    "tools": inbound_tools,
                    "headers": inbound_headers,
                },
            )
        )

        inbound_legacy = _modern_probe(
            uv,
            url=multirag_current["url"],
            mode="legacy",
            token="eim-f2-multirag",
            tool="list_datasets",
        )
        inbound_legacy_headers = _read_json(multirag_current["headers_url"])
        inbound_legacy_ok = (
            inbound_legacy.get("protocol_version") == "2025-11-25"
            and inbound_legacy.get("tools") == ["list_datasets", "multirag_retrieval"]
            and inbound_legacy.get("tool_result") == {"result": []}
            and inbound_legacy_headers.get("mcp-protocol-version") == "2025-11-25"
            and "mcp-method" not in inbound_legacy_headers
            and "mcp-name" not in inbound_legacy_headers
        )
        rows.append(
            _row(
                "legacy-mode-client-to-multirag",
                "MCP Python SDK 2 legacy mode",
                "MultiRAG FastMCP 4 dual-era",
                str(inbound_legacy.get("protocol_version")),
                "Streamable HTTP",
                inbound_legacy_ok,
                "legacy initialize remains an explicit compatibility path",
                {
                    "protocol_version": inbound_legacy.get("protocol_version"),
                    "tools": inbound_legacy.get("tools"),
                    "headers": inbound_legacy_headers,
                },
            )
        )

        legacy_http_result = _current_probe(url=legacy_http["url"], transport="streamable-http", scenario="echo")
        rows.append(
            _row(
                "legacy-http-success",
                "MultiRAG MCP SDK 2 auto",
                "FastMCP 3",
                str(legacy_http_result.get("protocol_version")),
                "Streamable HTTP",
                legacy_http_result.get("protocol_version") == "2025-11-25" and legacy_http_result.get("structured_content", {}).get("server_era") == "legacy",
                "server/discover falls back to initialize",
                {
                    "protocol_version": legacy_http_result.get("protocol_version"),
                    "structured_content": legacy_http_result.get("structured_content"),
                },
            )
        )

        modern_result = _modern_probe(uv, url=modern["url"], mode="2026-07-28")
        modern_headers = modern_result.get("echo", {}).get("request_headers", {})
        modern_ok = (
            modern_result.get("protocol_version") == "2026-07-28"
            and modern_headers.get("mcp-method") == "tools/call"
            and modern_headers.get("mcp-name") == "compat_echo"
            and "mcp-session-id" not in modern_headers
        )
        rows.append(
            _row(
                "modern-self-probe",
                "MCP Python SDK 2",
                "MCPServer 2",
                "2026-07-28",
                "stateless HTTP",
                modern_ok,
                "server/discover + routing headers + no session",
                {"protocol_version": modern_result.get("protocol_version"), "headers": modern_headers},
            )
        )

        current_to_modern = _current_probe(url=modern["url"], transport="streamable-http", scenario="echo")
        current_headers = current_to_modern.get("structured_content", {}).get("request_headers", {})
        current_modern_ok = (
            current_to_modern.get("protocol_version") == "2026-07-28"
            and current_headers.get("mcp-protocol-version") == "2026-07-28"
            and current_headers.get("mcp-method") == "tools/call"
            and current_headers.get("mcp-name") == "compat_echo"
            and "mcp-session-id" not in current_headers
        )
        rows.append(
            _row(
                "multirag-client-to-modern-server",
                "MultiRAG MCP SDK 2 auto",
                "MCPServer 2 dual-era",
                str(current_to_modern.get("protocol_version")),
                "Streamable HTTP",
                current_modern_ok,
                "server/discover selects sessionless modern protocol",
                current_headers,
            )
        )

        auto_fallback = _modern_probe(uv, url=legacy_http["url"], mode="auto")
        auto_ok = auto_fallback.get("protocol_version") != "2026-07-28" and auto_fallback.get("echo", {}).get("server_era") == "legacy"
        rows.append(
            _row(
                "modern-client-auto-fallback",
                "MCP Python SDK 2 auto",
                "FastMCP 3 legacy-only",
                str(auto_fallback.get("protocol_version")),
                "Streamable HTTP",
                auto_ok,
                "server/discover fallback to initialize",
                {"protocol_version": auto_fallback.get("protocol_version")},
            )
        )

        valid_auth = _current_probe(
            url=modern["protected_url"],
            transport="streamable-http",
            scenario="auth",
            auth="valid",
        )
        rows.append(
            _row(
                "auth-valid",
                "MultiRAG MCP SDK 2 auto",
                "MCPServer 2 protected fixture",
                str(valid_auth.get("protocol_version")),
                "Streamable HTTP",
                valid_auth.get("structured_content", {}).get("server_era") == "modern",
                "fixture bearer accepted without exposing credential",
                {"server_era": valid_auth.get("structured_content", {}).get("server_era")},
            )
        )

        for status, auth in ((401, "none"), (403, "insufficient")):
            raw_status = _raw_status(
                modern["protected_url"],
                None if auth == "none" else "eim-f2-insufficient",
            )
            auth_result = _current_probe(
                url=modern["protected_url"],
                transport="streamable-http",
                scenario="auth",
                auth=auth,
            )
            auth_meta = auth_result.get("meta") or {}
            expected_text = "Authentication required" if status == 401 else "Permission denied"
            rows.append(
                _row(
                    f"auth-{status}",
                    "MultiRAG MCP SDK 2 auto",
                    "MCPServer 2 protected fixture",
                    "connection rejected before negotiation",
                    "Streamable HTTP",
                    raw_status == status and auth_meta.get("connection_status") == status and expected_text in auth_result.get("text", ""),
                    f"fixture and wrapper preserve HTTP {status} category without credential data",
                    {
                        "http_status": raw_status,
                        "client_category": "connection_error",
                        "connection_status": auth_meta.get("connection_status"),
                        "initial_ready": auth_result.get("initial_ready"),
                    },
                )
            )

        tool_error = _current_probe(url=modern["url"], transport="streamable-http", scenario="fail")
        error_meta = tool_error.get("meta") or {}
        rows.append(
            _row(
                "tool-error",
                "MultiRAG MCP SDK 2 auto",
                "MCPServer 2",
                str(tool_error.get("protocol_version")),
                "Streamable HTTP",
                error_meta.get("is_error") is True,
                "text result plus side-channel is_error",
                {"is_error": error_meta.get("is_error"), "text": tool_error.get("text")},
            )
        )

        timeout_result = _current_probe(url=modern["url"], transport="streamable-http", scenario="timeout")
        server_status = timeout_result.get("server_status", {}).get("status")
        timeout_ok = "Timeout calling tool" in timeout_result.get("text", "") and server_status in {
            "cancelled",
            "completed",
        }
        rows.append(
            _row(
                "timeout-bounds-local-wait",
                "MultiRAG MCP SDK 2 auto",
                "MCPServer 2",
                str(timeout_result.get("protocol_version")),
                "Streamable HTTP",
                timeout_ok,
                "local wait is cancelled; remote handler cancellation remains cooperative",
                {
                    "server_status": server_status,
                    "duration_ms": timeout_result.get("duration_ms"),
                },
            )
        )

        cancel_result = _modern_probe(uv, url=modern["url"], mode="2026-07-28", cancel=True)
        cancel_status = cancel_result.get("server_status_after_cancel", {}).get("status")
        cancel_final = cancel_result.get("server_status_final", {}).get("status")
        cancel_observed = (cancel_status == "cancelled" and cancel_final == "cancelled") or (cancel_status == "running" and cancel_final == "completed")
        rows.append(
            _row(
                "modern-caller-cancel",
                "MCP Python SDK 2",
                "MCPServer 2",
                "2026-07-28",
                "stateless HTTP",
                cancel_result.get("caller_cancelled") is True and cancel_observed,
                "caller cancellation path recorded through terminal server status",
                {
                    "server_status_after_cancel": cancel_status,
                    "server_status_final": cancel_final,
                },
            )
        )
    finally:
        for server in reversed(servers):
            server.stop()
    return rows


def _print_markdown(rows: list[dict[str, Any]]) -> None:
    print("| ID | Client | Server | Protocol | Transport | Status | Outcome |")
    print("|---|---|---|---|---|:---:|---|")
    for row in rows:
        print(f"| {row['id']} | {row['client']} | {row['server']} | {row['protocol']} | {row['transport']} | {row['status']} | {row['outcome']} |")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="emit only the JSON report")
    args = parser.parse_args()
    rows = run_matrix()
    if not args.json:
        _print_markdown(rows)
    print(json.dumps({"rows": rows}, ensure_ascii=False, sort_keys=True))
    if any(row["status"] != "PASS" for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
