"""The `make smoke` gate, run against a test-owned listener via SMOKE_BASE_URL."""

import shutil
import sys

from tests.support.integration_suites import ROOT


def smoke_command(*make_variables: str) -> list[str]:
    # Hosts without make (Windows) run the target's command, as AGENTS.md prescribes.
    # That command already runs this interpreter unsynced, so make variable
    # overrides such as UV=uv run --no-sync have no counterpart there.
    return ["make", "smoke", *make_variables] if shutil.which("make") else [sys.executable, str(ROOT / "scripts" / "smoke.py")]
