"""The `make smoke` gate, run against a test-owned listener via SMOKE_BASE_URL."""

import shutil
import sys

from tests.support.integration_suites import ROOT


def smoke_command() -> list[str]:
    # Hosts without make (Windows) run the target's command, as AGENTS.md prescribes.
    return ["make", "smoke"] if shutil.which("make") else [sys.executable, str(ROOT / "scripts" / "smoke.py")]
