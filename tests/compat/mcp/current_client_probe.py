"""Run one production-client compatibility probe in an isolated process."""

import argparse
import json
import logging
import time
from types import SimpleNamespace
from typing import Any

from common.constants import MCPServerType
from common.mcp_tool_call_conn import MCPToolCallSession

_FIXTURE_TOKENS = {
    "none": None,
    "valid": "eim-f2-valid",
    "insufficient": "eim-f2-insufficient",
}


def _structured(session: MCPToolCallSession) -> Any:
    meta = session.get_last_tool_call_meta()
    return None if meta is None else meta.get("structured_content")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--transport", choices=("streamable-http", "sse"), required=True)
    parser.add_argument("--scenario", choices=("echo", "fail", "timeout", "auth"), required=True)
    parser.add_argument("--auth", choices=tuple(_FIXTURE_TOKENS), default="none")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)

    token = _FIXTURE_TOKENS[args.auth]
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    session = MCPToolCallSession(
        SimpleNamespace(
            id=f"eim-f2-{args.scenario}",
            url=args.url,
            headers=headers,
            server_type=MCPServerType(args.transport),
        )
    )
    initial_ready = session.wait_ready(3)
    started = time.monotonic()
    output: dict[str, Any] = {
        "initial_ready": initial_ready,
        "protocol_version": session.protocol_version,
        "scenario": args.scenario,
        "transport": args.transport,
    }

    if args.scenario in {"echo", "auth"}:
        if args.scenario == "echo":
            output["tools"] = sorted(tool.name for tool in session.get_tools(timeout=1))
        output["text"] = session.tool_call("compat_echo", {"value": args.scenario}, timeout=1)
        output["structured_content"] = _structured(session)
        output["meta"] = session.get_last_tool_call_meta()
    elif args.scenario == "fail":
        output["text"] = session.tool_call("compat_fail", {}, timeout=1)
        output["meta"] = session.get_last_tool_call_meta()
    else:
        call_id = "legacy-timeout"
        output["text"] = session.tool_call(
            "compat_wait",
            {"call_id": call_id, "delay_ms": 250},
            timeout=0.05,
        )
        time.sleep(0.3)
        output["status_text"] = session.tool_call("compat_status", {"call_id": call_id}, timeout=1)
        output["server_status"] = _structured(session)

    output["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
    output["eventual_init_error"] = session._init_error
    session.close_sync(timeout=2)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
