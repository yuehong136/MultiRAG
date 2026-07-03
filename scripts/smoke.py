"""冒烟测试：对运行中的 API 服务器打健康端点。

用法：
    uv run python scripts/smoke.py                  # 打已运行的服务器
    uv run python scripts/smoke.py --wait 120       # 轮询等待服务器就绪（CI 自拉起场景）
    uv run python scripts/smoke.py --strict         # 额外要求 healthz 整体 200

判定规则：
  - /api/v1/system/ping 必须 200 且包含 pong        —— 硬性要求
  - /api/v1/system/healthz 必须可达并返回 JSON，
    且其中 db 组件为 ok                              —— 硬性要求
  - healthz 整体 200（含 chat 等外部依赖组件）        —— 仅 --strict 时要求

Base URL 解析顺序：环境变量 SMOKE_BASE_URL >
configs/service_conf.yaml 的 multirag.host/http_port
（configs/local.service_conf.yaml 覆盖优先，host 为 0.0.0.0 时用 127.0.0.1）。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 允许以脚本方式直接运行

import requests


def resolve_base_url() -> str:
    env = os.environ.get("SMOKE_BASE_URL")
    if env:
        return env.rstrip("/")
    from common.config_utils import CONFIGS

    conf = CONFIGS.get("multirag") or {}
    host = conf.get("host", "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = conf.get("http_port", 8123)
    return f"http://{host}:{port}"


def wait_for_ping(base: str, timeout_sec: int) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{base}/api/v1/system/ping", timeout=3).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="MultiRAG smoke test")
    parser.add_argument("--wait", type=int, default=0, metavar="N", help="最多等待 N 秒直到服务器可达（默认不等待）")
    parser.add_argument("--strict", action="store_true", help="要求 healthz 整体 200（含 chat 等外部依赖）")
    args = parser.parse_args()

    base = resolve_base_url()
    print(f"smoke target: {base}")

    if args.wait and not wait_for_ping(base, args.wait):
        print(f"FAIL: 等待 {args.wait}s 后服务器仍不可达（{base}）", file=sys.stderr)
        print("启动方式：uv run python -m api.multirag_server", file=sys.stderr)
        return 1

    failures: list[str] = []

    # 1) ping
    try:
        resp = requests.get(f"{base}/api/v1/system/ping", timeout=5)
        ping_ok = resp.status_code == 200 and "pong" in resp.text.lower()
        print(f"  ping    : {'PASS' if ping_ok else 'FAIL'} (HTTP {resp.status_code})")
        if not ping_ok:
            failures.append(f"ping 返回 HTTP {resp.status_code}: {resp.text[:200]}")
    except requests.RequestException as exc:
        print("  ping    : FAIL (unreachable)")
        failures.append(f"ping 不可达: {exc}")

    # 2) healthz
    try:
        resp = requests.get(f"{base}/api/v1/system/healthz", timeout=30)
        try:
            body = resp.json()
        except json.JSONDecodeError:
            body = {}
            failures.append(f"healthz 未返回 JSON: {resp.text[:200]}")
        components = {k: v for k, v in body.items() if not k.startswith("_")}
        print(f"  healthz : HTTP {resp.status_code} {components}")
        if components.get("db") != "ok":
            failures.append(f"healthz db 组件异常: {body.get('db')!r}（详情 _meta: {body.get('_meta')!r}）")
        if args.strict and resp.status_code != 200:
            failures.append(f"--strict 模式要求 healthz 整体 200，实际 {resp.status_code}")
    except requests.RequestException as exc:
        print("  healthz : FAIL (unreachable)")
        failures.append(f"healthz 不可达: {exc}")

    if failures:
        print("\nSMOKE FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        if any("不可达" in f for f in failures):
            print("\n服务器未运行？启动方式：uv run python -m api.multirag_server", file=sys.stderr)
        return 1

    print("\nSMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
