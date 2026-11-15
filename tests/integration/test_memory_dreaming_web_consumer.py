"""The pinned Web client creates, reviews and publishes owned memory candidates."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.integration.test_memory_dreaming_http import dreaming_api as dreaming_api
from tests.integration.test_memory_writes_http import writes_api as writes_api
from tests.support.agent_update_release import release_api as release_api
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file

pytestmark = pytest.mark.external_consumer


def test_pinned_web_dreaming_review_and_publication(dreaming_api: dict[str, Any], tmp_path: Path) -> None:
    env = dreaming_api
    env["proposals"] = [env["proposals"][0]]
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("memory-dreaming-web-consumer")
    configuration, output = tmp_path / "dreaming-consumer.private.json", tmp_path / "dreaming-consumer-result.json"
    owned: list[Path] = []
    try:
        private_consumer_file(
            configuration,
            json.dumps({"baseURL": env["base"], "token": env["jwts"][1], "memoryId": env["memory_id"], "sourceIds": ["900"], "entryIds": [], "ownedScratchConfirmed": True, "resultFile": str(output)}),
            journal,
            owned,
        )
        private_consumer_file(output, "", journal, owned)
        assert configuration.stat().st_mode & 0o777 == output.stat().st_mode & 0o777 == 0o600
        journal.event("reserve-dreaming-web-consumer", web_sha=web_sha, memory_id=env["memory_id"], script="scripts/consumers/memory-dreaming.ts", output=str(output))
        completed = subprocess.run(
            ["node", "--import", "tsx", "scripts/consumers/memory-dreaming.ts", str(configuration)],
            cwd=checkout,
            env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"},
            text=True,
            capture_output=True,
            timeout=180,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        receipt = json.loads(output.read_text())
        assert receipt["passed"] is True and len(env["analysis_calls"]) == 1
        journal.event("dreaming-web-consumer-readback", web_sha=web_sha, receipt=receipt)
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("dreaming-web-private-files-cleaned", paths=[str(path) for path in owned], absent=True)
