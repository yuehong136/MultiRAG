"""Small HTTP helpers shared by the examples (not a MultiRAG SDK)."""

import json
import os
import signal
import time
from typing import Any

import requests


def terminate(signum: int, frame: Any) -> None:
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, terminate)


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"Set {name}; see examples/README.md")
    return value


def request(method: str, path: str, payload: dict[str, Any] | None = None, *, stream: bool = False) -> Any:
    base = os.environ.get("MULTIRAG_BASE_URL", "http://127.0.0.1:8123").rstrip("/")
    timeout = float(os.environ.get("MULTIRAG_TIMEOUT_SECONDS", "30"))
    if not 0 < timeout <= 300:
        raise ValueError("MULTIRAG_TIMEOUT_SECONDS must be in (0, 300]")
    headers = {"Authorization": f"Bearer {required('MULTIRAG_API_KEY')}"}
    with requests.request(method, base + "/api/v1" + path, headers=headers, json=payload, timeout=(min(5, timeout), min(15, timeout)), stream=stream) as response:
        if response.status_code != 200:
            raise RuntimeError(f"{method} {path}: HTTP {response.status_code}")
        if not stream:
            body = response.json()
            if body.get("code") != 0:
                raise RuntimeError(f"{method} {path}: business code {body.get('code')}")
            return body.get("data")
        if not response.headers.get("Content-Type", "").startswith("text/event-stream"):
            raise RuntimeError("Expected SSE response")
        deadline = time.monotonic() + timeout
        done = False
        for line in response.iter_lines(chunk_size=1, decode_unicode=True):
            if time.monotonic() >= deadline:
                raise TimeoutError("SSE deadline exceeded")
            if not line.startswith("data:"):
                continue
            frame = json.loads(line[5:])
            if frame.get("code") != 0:
                raise RuntimeError(f"SSE business code {frame.get('code')}")
            if frame["data"] is True:
                done = True
                break
            print(json.dumps(frame["data"], ensure_ascii=False))
        if not done:
            raise RuntimeError("SSE ended without success terminator")
        return None


def show(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))
