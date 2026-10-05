"""Run owned product checks; save results and light/dark contact sheets.

Default: automatically start the installed sibling Web checkout.
No resident API credentials, production writes or stored browser sessions needed.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-only", action="store_true", help="Explicitly omit the Web/visual checks; report partial scope")
    parser.add_argument("--web-checkout", type=Path, default=ROOT.parent / "web", help="Installed Web checkout to start on a private port")
    parser.add_argument("--web-base-url", help="Use an already-running Web build instead of starting Vite")
    parser.add_argument("--headed", action="store_true", help="Show Chromium while executing Web operations")
    parser.add_argument("--report-dir", type=Path, help="New evidence directory (default .test-results/<run-id>)")
    args = parser.parse_args(argv)
    if args.report_dir and args.report_dir.exists():
        if not args.report_dir.is_dir() or any(args.report_dir.iterdir()):
            parser.error("--report-dir must be an empty directory; keep each run's evidence independent")
    env = {**os.environ, "MULTIRAG_ACCEPTANCE_MODE": "api" if args.api_only else "full", "MULTIRAG_ACCEPTANCE_WEB_CHECKOUT": str(args.web_checkout.resolve())}
    if args.web_base_url:
        env["MULTIRAG_ACCEPTANCE_WEB_BASE_URL"] = args.web_base_url
    if args.headed:
        env["MULTIRAG_ACCEPTANCE_HEADED"] = "1"
    command = [sys.executable, str(ROOT / "scripts" / "run_integration.py"), "--suite", "acceptance", "--workers", "0"]
    if args.report_dir:
        command.extend(["--report-dir", str(args.report_dir.resolve())])
    command.extend(["--", "-q", "-s"])
    return subprocess.call(command, cwd=ROOT, env=env)


if __name__ == "__main__":
    sys.exit(main())
